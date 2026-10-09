import gzip
import json
from datetime import datetime, timezone

import pytest

from core import capture_clients


CAPTURE_ID = "3d2ed84e-9413-4a4a-a7e1-c596201bf84d"
PAYLOAD = {
    "capture_id": CAPTURE_ID,
    "title": "Song",
    "artist": "Artist",
    "apple_music_id": "123",
    "shazam_url": "https://www.shazam.com/track/123/song",
}
SELECTED = {"id": "track", "external_ids": {"isrc": "USAAA2600001"}}


@pytest.fixture
def objects(monkeypatch):
    stored = {}

    monkeypatch.setattr(capture_clients.archive, "get", lambda settings, key: stored.get(key))
    monkeypatch.setattr(
        capture_clients.archive, "put", lambda settings, key, value: stored.__setitem__(key, value)
    )
    monkeypatch.setattr(
        capture_clients.archive,
        "keys",
        lambda settings, prefix: sorted(key for key in stored if key.startswith(prefix)),
    )
    monkeypatch.setattr(
        capture_clients.archive, "delete", lambda settings, key: stored.pop(key, None)
    )
    return stored


NOW = datetime(2026, 10, 9, 22, 0, tzinfo=timezone.utc)


def receipt(objects, client="client-id", capture=CAPTURE_ID):
    return json.loads(
        gzip.decompress(objects[f"{capture_clients.RECEIPTS_PREFIX}/{client}/{capture}.json.gz"])
    )


def queued(objects):
    return [key for key in objects if key.startswith(capture_clients.QUEUE_PREFIX + "/")]


def spotify_429(retry_after="120"):
    import httpx

    response = httpx.Response(
        429,
        headers={"Retry-After": retry_after},
        request=httpx.Request("GET", "https://api.spotify.com/v1/search"),
    )
    return httpx.HTTPStatusError("429", request=response.request, response=response)


def test_accept_stores_the_capture_and_queues_it_without_touching_spotify(settings, objects):
    view = capture_clients.accept(settings, "client-id", PAYLOAD, NOW)

    assert view == {
        "capture_id": CAPTURE_ID,
        "status": "queued",
        "spotify_outcome": "not_added",
        "isrc": None,
        "title": "Song",
        "artist": "Artist",
        "retry_at": None,
        "reason": None,
    }
    state = receipt(objects)
    assert state["payload"] == PAYLOAD and state["status"] == "queued"
    assert state["received_at"] == "2026-10-09T22:00:00.000Z"
    assert queued(objects) == [f"{capture_clients.QUEUE_PREFIX}/client-id/{CAPTURE_ID}.json.gz"]
    assert capture_clients.status(settings, "client-id", CAPTURE_ID) == view
    assert capture_clients.status(settings, "other-client", CAPTURE_ID) is None


def test_accept_is_idempotent_and_refuses_a_changed_payload(settings, objects):
    first = capture_clients.accept(settings, "client-id", PAYLOAD, NOW)
    repeat = capture_clients.accept(settings, "client-id", PAYLOAD, NOW.replace(hour=23))

    assert repeat == first
    assert receipt(objects)["received_at"] == "2026-10-09T22:00:00.000Z", (
        "a retry keeps the first receipt time"
    )
    with pytest.raises(capture_clients.Conflict):
        capture_clients.accept(settings, "client-id", {**PAYLOAD, "title": "Other"}, NOW)
    with pytest.raises(capture_clients.InvalidRequest):
        capture_clients.accept(settings, "client-id", {**PAYLOAD, "capture_id": "bad"}, NOW)


def test_drain_delivers_queued_captures_in_arrival_order_and_records_the_outcome(settings, objects):
    later = "a4af8b79-a4c9-4b7a-a616-553021037845"
    capture_clients.accept(
        settings, "client-id", {**PAYLOAD, "capture_id": later}, NOW.replace(minute=5)
    )
    capture_clients.accept(settings, "client-id", PAYLOAD, NOW)
    order = []

    def step(client_id, payload, received_at):
        order.append((payload["capture_id"], received_at))
        return capture_clients.deliver(
            settings,
            client_id,
            payload,
            lambda p: SELECTED,
            lambda p, selected, record: {"ok": True, "message": "added", "isrc": "USAAA2600001"},
        )

    summary = capture_clients.drain(settings, step, NOW.replace(minute=10))

    assert order == [(CAPTURE_ID, "2026-10-09T22:00:00.000Z"), (later, "2026-10-09T22:05:00.000Z")]
    assert summary["added"] == 2 and queued(objects) == []
    view = capture_clients.status(settings, "client-id", CAPTURE_ID)
    assert view["status"] == "added" and view["spotify_outcome"] == "added"
    assert view["isrc"] == "USAAA2600001" and view["title"] == "Song"
    assert receipt(objects)["payload"] == PAYLOAD, (
        "the delivered receipt keeps the original capture"
    )
    assert (
        capture_clients.drain(settings, lambda *a: pytest.fail("delivered twice"), NOW)["processed"]
        == 0
    )


def test_no_exact_match_is_rechecked_daily_not_every_drain(settings, objects):
    capture_clients.accept(settings, "client-id", PAYLOAD, NOW)
    searches = []

    def step(client_id, payload, received_at):
        return capture_clients.deliver(
            settings,
            client_id,
            payload,
            lambda p: searches.append(1),
            lambda *a: pytest.fail("no selection reached Spotify"),
        )

    capture_clients.drain(settings, step, NOW)
    view = capture_clients.status(settings, "client-id", CAPTURE_ID)
    assert view["status"] == "not_added" and view["reason"] == "no_match"
    assert view["spotify_outcome"] == "not_added"
    assert view["retry_at"] == "2026-10-10T22:00:00.000Z"
    capture_clients.drain(settings, step, NOW.replace(hour=23))
    assert searches == [1], "rechecked once a day"
    capture_clients.drain(settings, step, datetime(2026, 10, 10, 22, 1, tzinfo=timezone.utc))
    assert searches == [1, 1]


def test_spotify_rate_limit_defers_the_whole_queue_until_retry_after(settings, objects):
    capture_clients.accept(settings, "client-id", PAYLOAD, NOW)
    capture_clients.accept(
        settings,
        "client-id",
        {**PAYLOAD, "capture_id": "a4af8b79-a4c9-4b7a-a616-553021037845"},
        NOW,
    )
    attempts = []

    def throttled(client_id, payload, received_at):
        attempts.append(payload["capture_id"])

        def select(p):
            raise spotify_429("120")

        return capture_clients.deliver(settings, client_id, payload, select, lambda *a: None)

    capture_clients.drain(settings, throttled, NOW)
    assert len(attempts) == 1, "a throttled Spotify stops the drain"
    view = capture_clients.status(settings, "client-id", attempts[0])
    assert view["status"] == "queued" and view["retry_at"] == "2026-10-09T22:02:00.000Z"
    assert view["spotify_outcome"] == "not_added"
    capture_clients.drain(settings, throttled, NOW.replace(minute=1))
    assert len(attempts) == 1, "nothing calls Spotify before its Retry-After"
    capture_clients.drain(settings, throttled, NOW.replace(minute=3))
    assert len(attempts) == 2


def test_failures_back_off_and_an_add_already_made_is_reported_added(settings, objects):
    capture_clients.accept(settings, "client-id", PAYLOAD, NOW)

    def add_then_catalog_fails(client_id, payload, received_at):
        def perform(p, selected, record):
            record("unknown")
            record("added")
            raise RuntimeError("catalog unavailable")

        return capture_clients.deliver(settings, client_id, payload, lambda p: SELECTED, perform)

    capture_clients.drain(settings, add_then_catalog_fails, NOW)
    view = capture_clients.status(settings, "client-id", CAPTURE_ID)
    assert view["status"] == "added" and view["spotify_outcome"] == "added"
    assert view["retry_at"] is None
    assert receipt(objects)["retry_at"] == "2026-10-09T22:01:00.000Z", (
        "catalog maintenance retries after a minute"
    )
    assert queued(objects), "the capture stays queued until its catalog writes finish"
    capture_clients.drain(settings, add_then_catalog_fails, NOW.replace(minute=2))
    assert receipt(objects)["retry_at"] == "2026-10-09T22:04:00.000Z", "then backs off"


def test_revoked_client_capture_is_rejected_and_leaves_the_queue(settings, objects):
    capture_clients.accept(settings, "client-id", PAYLOAD, NOW)

    def revoked(client_id, payload, received_at):
        raise capture_clients.Unauthorized

    capture_clients.drain(settings, revoked, NOW)
    assert capture_clients.status(settings, "client-id", CAPTURE_ID)["status"] == "rejected"
    assert queued(objects) == []


def test_receipt_from_before_queueing_is_queued_with_its_selection_and_outcome(settings, objects):
    key = f"{capture_clients.RECEIPTS_PREFIX}/client-id/{CAPTURE_ID}.json.gz"
    objects[key] = gzip.compress(
        json.dumps(
            {
                "capture_id": CAPTURE_ID,
                "payload_hash": capture_clients._payload_hash(PAYLOAD),
                "selected_track": SELECTED,
                "spotify_outcome": "unknown",
            }
        ).encode()
    )

    view = capture_clients.accept(settings, "client-id", PAYLOAD, NOW)

    assert view["status"] == "queued" and view["spotify_outcome"] == "unknown"
    state = receipt(objects)
    assert state["selected_track"] == SELECTED and state["payload"] == PAYLOAD
    assert queued(objects)
    done = {"capture_id": CAPTURE_ID, "payload_hash": state["payload_hash"], "isrc": "USAAA2600001"}
    objects[key] = gzip.compress(json.dumps(done).encode())
    assert capture_clients.accept(settings, "client-id", PAYLOAD, NOW)["status"] == "added"


def test_issued_tokens_are_independent_hashed_and_independently_revocable(settings, objects):
    first = capture_clients.issue(settings, "phone")
    second = capture_clients.issue(settings, "replacement phone")

    assert first["token"] != second["token"]
    registry = json.loads(gzip.decompress(objects[capture_clients.CLIENTS_KEY]))
    serialized = json.dumps(registry)
    assert first["token"] not in serialized and second["token"] not in serialized
    assert capture_clients.authenticate(settings, first["token"]) == first["client_id"]
    assert capture_clients.authenticate(settings, second["token"]) == second["client_id"]

    assert capture_clients.revoke(settings, first["client_id"]) is True
    with pytest.raises(capture_clients.Unauthorized):
        capture_clients.authenticate(settings, first["token"])
    assert capture_clients.authenticate(settings, second["token"]) == second["client_id"]


@pytest.mark.parametrize("token", [None, "", "not-a-token"])
def test_missing_or_invalid_token_is_unauthorized(settings, objects, token):
    with pytest.raises(capture_clients.Unauthorized):
        capture_clients.authenticate(settings, token)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {**PAYLOAD, "capture_id": "not-a-uuid"},
        {**PAYLOAD, "title": ""},
        {**PAYLOAD, "artist": 4},
        {**PAYLOAD, "extra": True},
        {key: value for key, value in PAYLOAD.items() if key != "shazam_url"},
        {**PAYLOAD, "isrc": "bad"},
        {**PAYLOAD, "isrc": None},
    ],
)
def test_malformed_capture_body_is_rejected_before_capture(settings, objects, payload):
    with pytest.raises(capture_clients.InvalidRequest):
        capture_clients.deliver(
            settings,
            "client-id",
            payload,
            lambda request: pytest.fail("malformed body reached selection"),
            lambda request, selected, record: pytest.fail("malformed body reached capture"),
        )


def test_optional_isrc_is_normalized_and_part_of_replay_identity(settings, objects):
    payload = {**PAYLOAD, "isrc": "us-aaa-26-00001"}
    try:
        validated = capture_clients.validate_payload(payload)
    except capture_clients.InvalidRequest:
        validated = None
    assert validated == {**PAYLOAD, "isrc": "USAAA2600001"}

    capture_clients.deliver(
        settings,
        "client-id",
        payload,
        lambda request: SELECTED,
        lambda request, selected, record: {"ok": True, "isrc": "USAAA2600001"},
    )
    with pytest.raises(capture_clients.Conflict):
        capture_clients.deliver(
            settings,
            "client-id",
            PAYLOAD,
            lambda request: pytest.fail("changed replay repeated selection"),
            lambda request, selected, record: pytest.fail("changed replay reached capture"),
        )


def test_success_is_receipted_and_replayed_without_recapture(settings, objects):
    calls = []

    def perform(payload, selected, record):
        calls.append(payload)
        assert selected == SELECTED
        return {"ok": True, "message": "added", "isrc": "USAAA2600001"}

    first = capture_clients.deliver(
        settings, "client-id", PAYLOAD, lambda payload: SELECTED, perform
    )
    replay = capture_clients.deliver(
        settings,
        "client-id",
        PAYLOAD,
        lambda payload: pytest.fail("receipt replay repeated selection"),
        perform,
    )

    expected = {
        "ok": True,
        "capture_id": CAPTURE_ID,
        "isrc": "USAAA2600001",
        "spotify_outcome": "added",
    }
    assert first == expected and replay == expected
    assert calls == [PAYLOAD]
    receipt = json.loads(
        gzip.decompress(
            objects[f"{capture_clients.RECEIPTS_PREFIX}/client-id/{CAPTURE_ID}.json.gz"]
        )
    )
    assert receipt["capture_id"] == CAPTURE_ID and receipt["isrc"] == "USAAA2600001"


def test_reusing_capture_id_with_changed_payload_is_a_conflict(settings, objects):
    capture_clients.deliver(
        settings,
        "client-id",
        PAYLOAD,
        lambda payload: SELECTED,
        lambda payload, selected, record: {"ok": True, "isrc": "USAAA2600001"},
    )

    with pytest.raises(capture_clients.Conflict):
        capture_clients.deliver(
            settings,
            "client-id",
            {**PAYLOAD, "title": "Different song"},
            lambda payload: pytest.fail("conflicting replay repeated selection"),
            lambda payload, selected, record: pytest.fail("conflicting replay reached capture"),
        )


def test_failed_capture_is_not_acknowledged_and_keeps_selection(settings, objects):
    result = capture_clients.deliver(
        settings,
        "client-id",
        PAYLOAD,
        lambda payload: SELECTED,
        lambda payload, selected, record: {"ok": False, "message": "no match", "isrc": None},
    )

    assert result == {
        "ok": False,
        "message": "no match",
        "isrc": None,
        "capture_id": CAPTURE_ID,
        "spotify_outcome": "not_added",
    }
    state = json.loads(
        gzip.decompress(
            objects[f"{capture_clients.RECEIPTS_PREFIX}/client-id/{CAPTURE_ID}.json.gz"]
        )
    )
    assert state["selected_track"] == SELECTED and "isrc" not in state


def test_receipt_persistence_failure_never_acknowledges_success(settings, objects, monkeypatch):
    def fail_receipt(settings, key, value):
        decoded = json.loads(gzip.decompress(value))
        if key.startswith(capture_clients.RECEIPTS_PREFIX) and "isrc" in decoded:
            raise RuntimeError("R2 unavailable")
        objects[key] = value

    monkeypatch.setattr(capture_clients.archive, "put", fail_receipt)
    with pytest.raises(capture_clients.DeliveryFailure) as failure:
        capture_clients.deliver(
            settings,
            "client-id",
            PAYLOAD,
            lambda payload: SELECTED,
            lambda payload, selected, record: {"ok": True, "isrc": "USAAA2600001"},
        )
    assert failure.value.outcome == "added"
    assert str(failure.value.__cause__) == "R2 unavailable"
    state = json.loads(
        gzip.decompress(
            objects[f"{capture_clients.RECEIPTS_PREFIX}/client-id/{CAPTURE_ID}.json.gz"]
        )
    )
    assert state["selected_track"] == SELECTED and "isrc" not in state


def test_enrollment_link_keeps_the_token_in_the_fragment():
    from urllib.parse import parse_qs, urlsplit

    link = capture_clients.enrollment_link(
        "https://ws--capture-enroll.modal.run", "https://ws--capture-consumer.modal.run", "t/k+="
    )
    parts = urlsplit(link)
    assert parts.query == ""
    assert parse_qs(parts.fragment) == {
        "url": ["https://ws--capture-consumer.modal.run"],
        "token": ["t/k+="],
    }


def test_enroll_page_hands_the_fragment_to_the_app_scheme():
    from core.enroll_page import ENROLL_PAGE

    assert "offlineshazam://enroll?" in ENROLL_PAGE
    assert "location.hash" in ENROLL_PAGE


def test_clients_are_bound_to_a_workspace(settings, objects):
    friend = capture_clients.issue(settings, "friend phone", workspace="friend")
    mine = capture_clients.issue(settings, "my phone")
    assert capture_clients.client_workspace(settings, friend["client_id"]) == "friend"
    assert capture_clients.client_workspace(settings, mine["client_id"]) == "default"


@pytest.mark.parametrize("value", ["bad", "2026-09-01T12:00:00", "", None])
def test_recognition_time_rejects_unknown_or_naive_input(value):
    with pytest.raises(capture_clients.InvalidRequest):
        capture_clients.validate_payload({**PAYLOAD, "recognized_at": value})


def test_recognition_time_retains_source_offset_and_precision():
    value = "2026-09-01T08:12:34.123456-04:00"
    assert (
        capture_clients.validate_payload({**PAYLOAD, "recognized_at": value})["recognized_at"]
        == value
    )
