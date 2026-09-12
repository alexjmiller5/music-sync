import gzip
import json

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


@pytest.fixture
def objects(monkeypatch):
    stored = {}

    monkeypatch.setattr(capture_clients.archive, "get", lambda settings, key: stored.get(key))
    monkeypatch.setattr(
        capture_clients.archive, "put", lambda settings, key, value: stored.__setitem__(key, value)
    )
    return stored


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
    ],
)
def test_malformed_capture_body_is_rejected_before_capture(settings, objects, payload):
    with pytest.raises(capture_clients.InvalidRequest):
        capture_clients.deliver(
            settings,
            "client-id",
            payload,
            lambda request: pytest.fail("malformed body reached capture"),
        )


def test_success_is_receipted_and_replayed_without_recapture(settings, objects):
    calls = []

    def perform(payload):
        calls.append(payload)
        return {"ok": True, "message": "added", "isrc": "USAAA2600001"}

    first = capture_clients.deliver(settings, "client-id", PAYLOAD, perform)
    replay = capture_clients.deliver(settings, "client-id", PAYLOAD, perform)

    expected = {"ok": True, "capture_id": CAPTURE_ID, "isrc": "USAAA2600001"}
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
        lambda payload: {"ok": True, "isrc": "USAAA2600001"},
    )

    with pytest.raises(capture_clients.Conflict):
        capture_clients.deliver(
            settings,
            "client-id",
            {**PAYLOAD, "title": "Different song"},
            lambda payload: pytest.fail("conflicting replay reached capture"),
        )


def test_failed_capture_is_not_receipted(settings, objects):
    result = capture_clients.deliver(
        settings,
        "client-id",
        PAYLOAD,
        lambda payload: {"ok": False, "message": "no match", "isrc": None},
    )

    assert result == {"ok": False, "message": "no match", "isrc": None}
    assert not any(key.startswith(capture_clients.RECEIPTS_PREFIX) for key in objects)


def test_receipt_persistence_failure_never_acknowledges_success(settings, objects, monkeypatch):
    def fail_receipt(settings, key, value):
        if key.startswith(capture_clients.RECEIPTS_PREFIX):
            raise RuntimeError("R2 unavailable")
        objects[key] = value

    monkeypatch.setattr(capture_clients.archive, "put", fail_receipt)
    with pytest.raises(RuntimeError, match="R2 unavailable"):
        capture_clients.deliver(
            settings,
            "client-id",
            PAYLOAD,
            lambda payload: {"ok": True, "isrc": "USAAA2600001"},
        )
