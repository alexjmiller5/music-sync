import app
import pytest

from tests.test_metadata_replay import KEY, STAMP, replay_env as replay_env


CAPTURE_ID = "3d2ed84e-9413-4a4a-a7e1-c596201bf84d"
CONSUMER_BODY = {
    "capture_id": CAPTURE_ID,
    "title": "Song",
    "artist": "Artist",
    "apple_music_id": "123",
    "shazam_url": "https://www.shazam.com/track/123/song",
}


def test_import_has_no_side_effects():
    assert app._run is not None


def test_reconcile_cron_skips_when_not_enabled(monkeypatch):
    monkeypatch.setattr(app.capture_drain, "spawn", lambda *args: None)
    monkeypatch.setattr(app.worker, "remote", app.worker.get_raw_f())
    monkeypatch.delenv("RECONCILE_ENABLED", raising=False)
    assert app.reconcile_cron.get_raw_f()() == {"skipped": True}


def test_flag_quietly_swallows_a_filing_failure(settings, monkeypatch):
    from core import flags

    def boom(*args, **kwargs):
        raise RuntimeError("notion outage")

    monkeypatch.setattr(flags, "file", boom)
    assert app._flag_quietly(settings, ["a flag"], ["an error"]) is None


def test_capture_auth_error_is_real_json_response_and_runtime_dependency(settings, monkeypatch):
    import json
    import tomllib
    from pathlib import Path

    from fastapi.responses import JSONResponse
    from core import config, spotify_client

    dependencies = tomllib.loads(Path("pyproject.toml").read_text())["project"]["dependencies"]
    assert any(dep.split(">")[0].split("=")[0] == "fastapi" for dep in dependencies)
    monkeypatch.setattr(config, "Settings", lambda: settings)
    monkeypatch.setattr("core.archive.get", lambda *args: None)
    monkeypatch.setattr(app, "_flag_quietly", lambda *args: None)

    def expired(*args, **kwargs):
        raise spotify_client.SpotifyAuthError("invalid_grant")

    monkeypatch.setattr(spotify_client, "SpotifyClient", expired)
    # Real endpoint serialization path, with only external service boundaries replaced.
    monkeypatch.setattr(app.worker, "remote", app.worker.get_raw_f())
    response = app.capture.get_raw_f()({"title": "Song", "artist": "Artist"})
    assert isinstance(response, JSONResponse)
    assert response.status_code == 503
    assert json.loads(response.body) == {"ok": False, "message": "Spotify token expired; flagged"}

    from fastapi import FastAPI
    import asyncio
    import httpx

    api = FastAPI()
    api.post("/capture")(app.capture.get_raw_f())

    async def request():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url="http://test"
        ) as client:
            return await client.post("/capture", json={"title": "Song", "artist": "Artist"})

    http_response = asyncio.run(request())
    assert http_response.status_code == 503
    assert http_response.json() == json.loads(response.body)


@pytest.fixture
def capture_api(settings, monkeypatch):
    import asyncio
    import httpx
    from datetime import datetime, timezone
    from fastapi import FastAPI
    from core import archive, capture_clients, config

    objects = {}
    monkeypatch.setattr(config, "Settings", lambda: settings)
    monkeypatch.setattr(archive, "get", lambda settings, key: objects.get(key))
    monkeypatch.setattr(
        archive, "put", lambda settings, key, value: objects.__setitem__(key, value)
    )
    monkeypatch.setattr(
        archive, "keys", lambda settings, prefix: sorted(k for k in objects if k.startswith(prefix))
    )
    monkeypatch.setattr(archive, "delete", lambda settings, key: objects.pop(key, None))
    monkeypatch.setattr(app.worker, "remote", app.worker.get_raw_f())
    spawned = []
    monkeypatch.setattr(app.capture_drain, "spawn", lambda *args: spawned.append(args))
    consumer = app.capture_consumer.get_raw_f()()
    operator = FastAPI()
    operator.post("/capture-access")(app.capture_access.get_raw_f())

    def request(method, path, body=None, token=None):
        target = operator
        if path.startswith("/capture-consumer"):
            target, path = consumer, path.removeprefix("/capture-consumer") or "/"

        async def send():
            headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=target), base_url="http://test"
            ) as client:
                return await client.request(method, path, json=body, headers=headers)

        return asyncio.run(send())

    def post(path, body, token=None):
        return request("POST", path, body, token)

    post.get = lambda path, token=None: request("GET", path, None, token)
    post.drain = lambda now=None: capture_clients.drain(
        settings, app._deliver_queued, now or datetime.now(timezone.utc)
    )
    post.spawned = spawned
    return post, objects


def issue_capture_token(post):
    response = post("/capture-access", {"action": "issue", "label": "phone"})
    assert response.status_code == 200
    return response.json()


def status_of(post, token, capture_id=CAPTURE_ID):
    response = post.get(f"/capture-consumer/{capture_id}", token)
    assert response.status_code == 200
    return response.json()


def later(seconds):
    from datetime import datetime, timedelta, timezone

    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


def added_capture(body, selected=None, settings=None, record=None, client_id=None, **kw):
    return {"ok": True, "message": "added", "isrc": "USAAA2600001"}


def test_consumer_capture_requires_valid_bearer_token(capture_api):
    post, _ = capture_api
    assert post("/capture-consumer", CONSUMER_BODY).status_code == 401
    assert post("/capture-consumer", CONSUMER_BODY, "invalid").status_code == 401
    assert post.get(f"/capture-consumer/{CAPTURE_ID}").status_code == 401


def test_capture_is_accepted_at_once_and_delivered_by_the_drain(capture_api, monkeypatch):
    import time
    from core import capture as capture_mod, hub

    post, _ = capture_api
    token = issue_capture_token(post)["token"]
    monkeypatch.setattr(
        capture_mod, "resolve_track", lambda *a: pytest.fail("accept reached Spotify")
    )
    monkeypatch.setattr(hub, "Hub", lambda *a: pytest.fail("accept read the catalog"))
    started = time.monotonic()
    response = post("/capture-consumer", CONSUMER_BODY, token)
    assert time.monotonic() - started < 1
    queued = {
        "capture_id": CAPTURE_ID,
        "status": "queued",
        "spotify_outcome": "not_added",
        "isrc": None,
        "title": "Song",
        "artist": "Artist",
        "retry_at": None,
        "reason": None,
    }
    assert response.status_code == 202 and response.json() == {"ok": True, **queued}
    assert len(post.spawned) == 1, "accepting starts a drain"
    assert status_of(post, token) == queued
    repeat = post("/capture-consumer", CONSUMER_BODY, token)
    assert repeat.status_code == 202 and repeat.json() == {"ok": True, **queued}

    monkeypatch.setattr(capture_mod, "resolve_track", lambda *a: {"id": "selected"})
    monkeypatch.setattr(app, "_capture", added_capture)
    assert post.drain()["added"] == 1
    assert status_of(post, token) == {
        **queued,
        "status": "added",
        "spotify_outcome": "added",
        "isrc": "USAAA2600001",
    }


def test_queue_delivery_uses_the_receipt_time_and_never_the_worker_mirror_cache(
    capture_api, monkeypatch
):
    import gzip
    import json
    from core import capture as capture_mod, capture_clients

    post, objects = capture_api
    token = issue_capture_token(post)["token"]
    monkeypatch.setattr(capture_mod, "resolve_track", lambda *args: {"id": "selected"})
    seen = {}

    def capture(payload, spotify, hub, settings, now, **kw):
        seen.update(kw, now=now)
        return {"ok": True, "message": "added", "isrc": "USAAA2600001"}

    monkeypatch.setattr(capture_mod, "capture", capture)
    post("/capture-consumer", CONSUMER_BODY, token)
    receipt = next(v for k, v in objects.items() if k.startswith(capture_clients.RECEIPTS_PREFIX))
    received_at = json.loads(gzip.decompress(receipt))["received_at"]
    post.drain(later(30))
    assert seen["use_cache"] is False
    assert seen["now"].isoformat().replace("+00:00", "")[:23] == received_at.rstrip("Z")


def test_status_is_private_to_the_client_that_sent_the_capture(capture_api):
    post, _ = capture_api
    first = issue_capture_token(post)["token"]
    second = issue_capture_token(post)["token"]
    assert post("/capture-consumer", CONSUMER_BODY, first).status_code == 202
    assert post.get(f"/capture-consumer/{CAPTURE_ID}", second).status_code == 404
    assert (
        post.get("/capture-consumer/a4af8b79-a4c9-4b7a-a616-553021037845", first).status_code == 404
    )
    assert post.get("/capture-consumer/not-a-uuid", first).status_code == 404


def test_operator_can_issue_and_revoke_one_consumer_without_affecting_another(
    capture_api, monkeypatch
):
    from core import capture as capture_mod

    post, _ = capture_api
    monkeypatch.setattr(capture_mod, "resolve_track", lambda *args: {"id": "selected"})
    monkeypatch.setattr(app, "_capture", added_capture)
    first = issue_capture_token(post)
    second = issue_capture_token(post)

    assert post("/capture-consumer", CONSUMER_BODY, first["token"]).status_code == 202
    post.drain()
    assert status_of(post, first["token"])["status"] == "added"
    revoked = post("/capture-access", {"action": "revoke", "client_id": first["client_id"]})
    assert revoked.status_code == 200 and revoked.json() == {"ok": True, "revoked": True}
    other = {**CONSUMER_BODY, "capture_id": "a4af8b79-a4c9-4b7a-a616-553021037845"}
    assert post("/capture-consumer", other, first["token"]).status_code == 401
    assert post("/capture-consumer", other, second["token"]).status_code == 202


def test_empty_capture_is_the_connection_check_cochlea_relies_on(capture_api):
    post, objects = capture_api
    token = issue_capture_token(post)["token"]
    response = post("/capture-consumer", {}, token)
    assert response.status_code == 422
    assert response.json() == {
        "ok": False,
        "message": "capture requires capture_id, title, artist, apple_music_id and shazam_url; "
        "isrc and recognized_at are optional",
    }
    assert post.spawned == [] and not any("capture-queue" in key for key in objects)


def test_consumer_capture_rejects_malformed_body_and_changed_replay(capture_api):
    post, _ = capture_api
    token = issue_capture_token(post)["token"]
    malformed = post("/capture-consumer", {**CONSUMER_BODY, "capture_id": "bad"}, token)
    assert malformed.status_code == 422 and malformed.json()["ok"] is False
    assert post("/capture-consumer", CONSUMER_BODY, token).status_code == 202
    conflict = post("/capture-consumer", {**CONSUMER_BODY, "title": "Other"}, token)
    assert conflict.status_code == 409 and conflict.json()["ok"] is False


def test_delivered_capture_replays_its_receipt_without_recapturing(capture_api, monkeypatch):
    from core import capture as capture_mod

    post, _ = capture_api
    token = issue_capture_token(post)["token"]
    calls = []
    monkeypatch.setattr(capture_mod, "resolve_track", lambda *args: {"id": "selected"})

    def perform(body, selected=None, settings=None, record=None, client_id=None, **kw):
        assert client_id
        calls.append(body)
        return {"ok": True, "message": "added", "isrc": "USAAA2600001"}

    monkeypatch.setattr(app, "_capture", perform)
    post("/capture-consumer", CONSUMER_BODY, token)
    post.drain()
    replay = post("/capture-consumer", CONSUMER_BODY, token)
    # Cochlea 0.5.0 marks a capture delivered on exactly this acknowledgement.
    assert replay.status_code == 200
    assert replay.json()["ok"] is True and replay.json()["capture_id"] == CAPTURE_ID
    assert replay.json()["isrc"] == "USAAA2600001" and replay.json()["spotify_outcome"] == "added"
    post.drain()
    assert len(calls) == 1


def test_consumer_capture_returns_safe_unavailable_when_receipt_write_fails(
    capture_api, monkeypatch
):
    from core import capture_clients

    post, objects = capture_api
    token = issue_capture_token(post)["token"]

    def fail_receipt(settings, key, value):
        if key.startswith(capture_clients.RECEIPTS_PREFIX):
            raise RuntimeError("sensitive storage detail")
        objects[key] = value

    monkeypatch.setattr(capture_clients.archive, "put", fail_receipt)
    response = post("/capture-consumer", CONSUMER_BODY, token)
    assert response.status_code == 503
    assert response.json() == {
        "ok": False,
        "message": "capture unavailable",
        "capture_id": CAPTURE_ID,
        "spotify_outcome": "not_added",
    }
    assert post.spawned == []


def test_spotify_rate_limit_holds_the_capture_until_retry_after(capture_api, monkeypatch):
    import httpx
    from core import capture as cap

    post, _ = capture_api
    token = issue_capture_token(post)["token"]
    searches = []

    def rate_limited(payload, spotify, settings):
        searches.append(1)
        response = httpx.Response(
            429,
            headers={"Retry-After": "120"},
            request=httpx.Request("GET", "https://api.spotify.com/v1/search"),
        )
        raise httpx.HTTPStatusError("429", request=response.request, response=response)

    monkeypatch.setattr(cap, "resolve_track", rate_limited)
    post("/capture-consumer", CONSUMER_BODY, token)
    post.drain()
    status = status_of(post, token)
    assert status["status"] == "queued" and status["spotify_outcome"] == "not_added"
    assert status["retry_at"] is not None
    repeat = post("/capture-consumer", CONSUMER_BODY, token)
    assert repeat.status_code == 202 and 110 <= int(repeat.headers["Retry-After"]) <= 120
    post.drain()
    assert searches == [1], "nothing reaches Spotify before its Retry-After"
    post.drain(later(121))
    assert searches == [1, 1]


def test_drain_rechecks_revocation_before_delivering(capture_api, settings, monkeypatch):
    from core import capture_clients

    post, _ = capture_api
    issued = issue_capture_token(post)
    captures = []
    monkeypatch.setattr(app, "_capture", lambda *a, **kw: captures.append(a) or added_capture(*a))
    post("/capture-consumer", CONSUMER_BODY, issued["token"])
    capture_clients.revoke(settings, issued["client_id"])
    post.drain()
    assert captures == []
    assert capture_clients.status(settings, issued["client_id"], CAPTURE_ID)["status"] == "rejected"


def test_receipt_failure_retry_keeps_first_selected_recording(capture_api, settings, monkeypatch):
    import gzip
    import json

    from core import archive, capture_clients, hub, spotify_client
    from tests.test_capture import FakeHub, FakeSpotify, INBOX, track

    post, objects = capture_api
    token = issue_capture_token(post)["token"]
    first = track("a", "Song", "Artist", isrc="USAAA2600001")
    second = track("b", "Song", "Artist", isrc="USAAA2600002")
    spotify = FakeSpotify([first])
    fake_hub = FakeHub(INBOX)
    monkeypatch.setattr(spotify_client, "SpotifyClient", lambda settings: spotify)
    monkeypatch.setattr(hub, "Hub", lambda *args: fake_hub)
    failed = False

    def fail_first_completed_receipt(settings, key, value):
        nonlocal failed
        decoded = json.loads(gzip.decompress(value))
        if key.startswith(capture_clients.RECEIPTS_PREFIX) and "isrc" in decoded and not failed:
            failed = True
            raise RuntimeError("R2 unavailable after capture")
        objects[key] = value

    monkeypatch.setattr(archive, "put", fail_first_completed_receipt)
    post("/capture-consumer", CONSUMER_BODY, token)
    post.drain()
    state_key = f"{capture_clients.RECEIPTS_PREFIX}/{capture_clients.authenticate(settings, token)}/{CAPTURE_ID}.json.gz"
    selected_state = json.loads(gzip.decompress(objects[state_key]))
    assert selected_state["selected_track"]["id"] == "a" and "isrc" not in selected_state
    assert status_of(post, token)["spotify_outcome"] == "added", "Spotify acknowledged the add"
    spotify.tracks = [second]

    post.drain(later(3600))

    status = status_of(post, token)
    assert status["status"] == "added" and status["isrc"] == "USAAA2600001"
    assert [call for call in spotify.calls if call[0] == "add"] == [
        ("add", "IN", ["spotify:track:a"])
    ]


def test_incomplete_selection_is_not_pinned_and_the_daily_recheck_can_resolve(
    capture_api, monkeypatch
):
    import gzip
    import json
    from core import capture_clients, hub, spotify_client
    from tests.test_capture import FakeHub, FakeSpotify, INBOX, track

    post, objects = capture_api
    token = issue_capture_token(post)["token"]
    spotify = FakeSpotify([track("a", "Song", "Artist", isrc=None)])
    fake_hub = FakeHub(INBOX)
    monkeypatch.setattr(spotify_client, "SpotifyClient", lambda settings: spotify)
    monkeypatch.setattr(hub, "Hub", lambda *args: fake_hub)

    post("/capture-consumer", CONSUMER_BODY, token)
    post.drain()

    status = status_of(post, token)
    assert status["status"] == "not_added" and status["reason"] == "no_match"
    receipt = next(v for k, v in objects.items() if k.startswith(capture_clients.RECEIPTS_PREFIX))
    assert "selected_track" not in json.loads(gzip.decompress(receipt))
    assert spotify.calls == [] and fake_hub.pushed == []

    spotify.tracks = [track("a", "Song", "Artist", isrc="USAAA2600001")]
    post.drain(later(86400 + 60))

    assert status_of(post, token)["status"] == "added"
    assert spotify.searches == [
        ("metadata", "Song", "Artist", "US"),
        ("metadata", "Song", "Artist", "US"),
    ]
    assert [call for call in spotify.calls if call[0] == "add"] == [
        ("add", "IN", ["spotify:track:a"])
    ]


@pytest.mark.parametrize(
    "failure,want",
    [
        ("before_add", "not_added"),
        ("add_timeout", "unknown"),
        ("after_add", "added"),
        (None, "added"),
    ],
)
def test_capture_status_tracks_spotify_effect_even_when_catalog_fails(
    capture_api, monkeypatch, failure, want
):
    import httpx
    from core import hub, spotify_client
    from tests.test_capture import FakeHub, FakeSpotify, INBOX, track

    post, _ = capture_api
    token = issue_capture_token(post)["token"]
    spotify = FakeSpotify([track("track", "Song", "Artist")])
    catalog = FakeHub(INBOX)

    if failure == "before_add":

        def broken_catalog():
            raise RuntimeError("catalog unavailable")

        catalog.catalog = broken_catalog
    elif failure == "add_timeout":

        def add_then_timeout(pid, uris):
            FakeSpotify.add_items(spotify, pid, uris)
            raise httpx.ReadTimeout("ack lost")

        spotify.add_items = add_then_timeout
    elif failure == "after_add":

        def broken_push(*args):
            raise RuntimeError("catalog unavailable")

        catalog.push = broken_push

    monkeypatch.setattr(spotify_client, "SpotifyClient", lambda *args: spotify)
    monkeypatch.setattr(hub, "Hub", lambda *args: catalog)
    post("/capture-consumer", CONSUMER_BODY, token)
    post.drain()
    status = status_of(post, token)
    assert status["spotify_outcome"] == want
    assert status["status"] == ("added" if want == "added" else "queued")
    assert bool(spotify.calls) == (failure != "before_add")

    if failure == "after_add":
        # The retry fails earlier this time. It must retain proof of the earlier add.
        def broken_catalog_again():
            raise RuntimeError("catalog unavailable")

        catalog.catalog = broken_catalog_again
        post.drain(later(3600))
        assert status_of(post, token)["spotify_outcome"] == "added"
        assert len(spotify.calls) == 1


@pytest.mark.parametrize("legacy_outcome", [None, "unknown", "added"])
def test_receipt_from_synchronous_delivery_cannot_report_not_added_after_a_possible_add(
    capture_api, monkeypatch, legacy_outcome
):
    import gzip
    import json
    from core import capture_clients, hub, spotify_client
    from tests.test_capture import FakeHub, FakeSpotify, INBOX, track

    post, objects = capture_api
    client = issue_capture_token(post)
    state = {
        "capture_id": CAPTURE_ID,
        "payload_hash": capture_clients._payload_hash(CONSUMER_BODY),
        "selected_track": track("track", "Song", "Artist"),
    }
    if legacy_outcome is not None:
        state["spotify_outcome"] = legacy_outcome
    key = f"{capture_clients.RECEIPTS_PREFIX}/{client['client_id']}/{CAPTURE_ID}.json.gz"
    objects[key] = gzip.compress(json.dumps(state).encode())
    catalog = FakeHub(INBOX)

    def fail_before_add():
        raise RuntimeError("catalog unavailable")

    catalog.catalog = fail_before_add
    spotify = FakeSpotify([])
    monkeypatch.setattr(spotify_client, "SpotifyClient", lambda *args: spotify)
    monkeypatch.setattr(hub, "Hub", lambda *args: catalog)
    accepted = post("/capture-consumer", CONSUMER_BODY, client["token"])
    assert accepted.json()["spotify_outcome"] == (legacy_outcome or "unknown")
    post.drain()
    assert status_of(post, client["token"])["spotify_outcome"] == (legacy_outcome or "unknown")
    assert spotify.calls == []


def test_failed_durable_attempt_marker_prevents_spotify_request(capture_api, monkeypatch):
    import gzip
    import json
    from core import archive, capture_clients, hub, spotify_client
    from tests.test_capture import FakeHub, FakeSpotify, INBOX, track

    post, objects = capture_api
    token = issue_capture_token(post)["token"]
    spotify = FakeSpotify([track("track", "Song", "Artist")])
    monkeypatch.setattr(spotify_client, "SpotifyClient", lambda *args: spotify)
    monkeypatch.setattr(hub, "Hub", lambda *args: FakeHub(INBOX))

    def put(settings, key, value):
        if key.startswith(capture_clients.RECEIPTS_PREFIX):
            if json.loads(gzip.decompress(value)).get("spotify_outcome") == "unknown":
                raise RuntimeError("receipt unavailable")
        objects[key] = value

    monkeypatch.setattr(archive, "put", put)
    post("/capture-consumer", CONSUMER_BODY, token)
    post.drain()
    status = status_of(post, token)
    assert status["status"] == "queued" and status["spotify_outcome"] == "not_added"
    assert spotify.calls == []


def test_existing_membership_is_reported_added_before_metadata_processing(capture_api, monkeypatch):
    from core import hub, metadata, spotify_client
    from tests.test_capture import FakeHub, FakeSpotify, INBOX, track

    post, _ = capture_api
    token = issue_capture_token(post)["token"]
    song = track("track", "Song", "Artist")
    spotify = FakeSpotify([song], [{"added_at": "2026-01-01T00:00:00Z", "item": song}])
    monkeypatch.setattr(spotify_client, "SpotifyClient", lambda *args: spotify)
    monkeypatch.setattr(hub, "Hub", lambda *args: FakeHub(INBOX))

    def fail_metadata(*args, **kwargs):
        raise RuntimeError("metadata unavailable")

    monkeypatch.setattr(metadata, "observation_actions", fail_metadata)
    post("/capture-consumer", CONSUMER_BODY, token)
    post.drain()
    status = status_of(post, token)
    assert status["status"] == "added" and status["spotify_outcome"] == "added"
    assert spotify.calls == []


def test_add_whose_receipt_write_failed_is_confirmed_by_the_next_drain(capture_api, monkeypatch):
    import gzip
    import json
    from core import archive, capture_clients, hub, spotify_client
    from tests.test_capture import FakeHub, FakeSpotify, INBOX, track

    post, objects = capture_api
    token = issue_capture_token(post)["token"]
    spotify = FakeSpotify([track("track", "Song", "Artist")])
    monkeypatch.setattr(spotify_client, "SpotifyClient", lambda *args: spotify)
    monkeypatch.setattr(hub, "Hub", lambda *args: FakeHub(INBOX))
    healthy = archive.put

    def put(settings, key, value):
        if key.startswith(capture_clients.RECEIPTS_PREFIX):
            if json.loads(gzip.decompress(value)).get("spotify_outcome") == "added":
                raise RuntimeError("receipt unavailable")
        objects[key] = value

    monkeypatch.setattr(archive, "put", put)
    post("/capture-consumer", CONSUMER_BODY, token)
    post.drain()
    assert status_of(post, token)["spotify_outcome"] == "unknown", "never claimed not added"
    assert len(spotify.calls) == 1
    monkeypatch.setattr(archive, "put", healthy)
    post.drain(later(3600))
    assert status_of(post, token)["status"] == "added"
    assert len(spotify.calls) == 1, "the inbox observation confirms it without another add"


def test_catalog_rate_limit_after_the_add_keeps_added_and_waits_its_retry_after(
    capture_api, monkeypatch
):
    import gzip
    import json
    import httpx
    from datetime import datetime, timezone
    from core import capture_clients, hub, spotify_client
    from tests.test_capture import FakeHub, FakeSpotify, INBOX, track

    post, objects = capture_api
    token = issue_capture_token(post)["token"]
    spotify = FakeSpotify([track("track", "Song", "Artist")])
    catalog = FakeHub(INBOX)

    def push(*args):
        response = httpx.Response(
            429, headers={"Retry-After": "600"}, request=httpx.Request("POST", "https://hub.test")
        )
        raise httpx.HTTPStatusError("limited", request=response.request, response=response)

    catalog.push = push
    monkeypatch.setattr(spotify_client, "SpotifyClient", lambda *args: spotify)
    monkeypatch.setattr(hub, "Hub", lambda *args: catalog)
    post("/capture-consumer", CONSUMER_BODY, token)
    now = datetime.now(timezone.utc)
    post.drain(now)
    assert status_of(post, token)["spotify_outcome"] == "added"
    receipt = next(v for k, v in objects.items() if k.startswith(capture_clients.RECEIPTS_PREFIX))
    retry_at = datetime.fromisoformat(
        json.loads(gzip.decompress(receipt))["retry_at"].replace("Z", "+00:00")
    )
    assert (retry_at - now).total_seconds() >= 599.9, "the hub's Retry-After is honoured"
    assert capture_clients.SPOTIFY_GATE_KEY not in objects, "only Spotify's limit gates the queue"


def test_definitive_no_match_tells_the_client_to_stay_away(capture_api, monkeypatch):
    from core import capture as cap

    post, _ = capture_api
    token = issue_capture_token(post)["token"]
    searches = []
    monkeypatch.setattr(cap, "resolve_track", lambda *args: searches.append(1))
    post("/capture-consumer", CONSUMER_BODY, token)
    post.drain()
    status = status_of(post, token)
    assert status["status"] == "not_added" and status["reason"] == "no_match"
    repeat = post("/capture-consumer", CONSUMER_BODY, token)
    assert repeat.status_code == 422
    assert repeat.json()["spotify_outcome"] == "not_added" and repeat.json()["reason"] == "no_match"
    # A client retrying every 30 s would search Spotify ~2,900 times a day.
    assert int(repeat.headers["Retry-After"]) >= 86000
    post.drain()
    assert searches == [1]


def test_transient_refusal_keeps_the_normal_retry(capture_api, monkeypatch):
    from core import capture as cap

    post, _ = capture_api
    token = issue_capture_token(post)["token"]
    monkeypatch.setattr(cap, "resolve_track", lambda *args: {"id": "selected"})
    monkeypatch.setattr(
        app,
        "_capture",
        lambda *args, **kw: {"ok": False, "message": "Pending recovery; retry later", "isrc": None},
    )
    post("/capture-consumer", CONSUMER_BODY, token)
    post.drain()
    assert status_of(post, token)["status"] == "queued"
    repeat = post("/capture-consumer", CONSUMER_BODY, token)
    assert repeat.status_code == 202 and int(repeat.headers["Retry-After"]) <= 60


def test_reconcile_cron_also_starts_a_capture_drain(monkeypatch):
    spawned = []
    monkeypatch.setattr(app.capture_drain, "spawn", lambda *args: spawned.append(args))
    monkeypatch.setattr(app.worker, "remote", lambda *args: {"skipped": True})
    app.reconcile_cron.get_raw_f()()
    assert spawned == [()]


@pytest.fixture
def replay_client(settings, replay_env, monkeypatch):
    import asyncio
    import httpx
    from fastapi import FastAPI
    from core import config, metadata_replay

    monkeypatch.delenv("RECONCILE_ENABLED", raising=False)
    monkeypatch.setattr(config, "Settings", lambda: settings)
    monkeypatch.setattr(metadata_replay, "Hub", lambda *args: replay_env[2])
    monkeypatch.setattr(app.worker, "remote", app.worker.get_raw_f())
    monkeypatch.setattr(app, "_run", lambda **kw: pytest.fail("replay fell through to reconcile"))
    api = FastAPI()
    api.post("/reconcile")(app.reconcile.get_raw_f())
    api.post("/capture")(app.capture.get_raw_f())

    def post(body, path="/reconcile"):
        async def request():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=api), base_url="http://test"
            ) as client:
                return await client.post(path, json=body)

        return asyncio.run(request())

    return post


@pytest.mark.parametrize("dry_run", ["omitted", True, False])
def test_replay_endpoint_works_while_cron_disabled_and_only_false_applies(
    replay_client, replay_env, dry_run
):
    body = {"metadata_replay": {"archive_key": KEY, "observed_at": STAMP}}
    if dry_run != "omitted":
        body["dry_run"] = dry_run
    response = replay_client(body)
    assert response.status_code == 200
    assert response.json()["dry_run"] is (dry_run is not False)
    hub = replay_env[2]
    assert bool(hub.pushes) is (dry_run is False)
    assert bool(hub.saves) is (dry_run is False)


@pytest.mark.parametrize(
    "change",
    [
        {"dry_run": "false"},
        {"dry_run": "true"},
        {"dry_run": None},
        {"dry_run": 0},
        {"dry_run": 1},
        {"dry_run": []},
        {"dry_run": {}},
        {"unknown": True},
        {"metadata_replay": None},
        {"metadata_replay": False},
        {"metadata_replay": []},
        {"metadata_replay": "source"},
        {"metadata_replay": {}},
        {"metadata_replay": {"archive_key": KEY}},
        {"metadata_replay": {"archive_key": KEY, "observed_at": STAMP, "dry_run": False}},
        {"metadata_replay": {"archive_key": 4, "observed_at": STAMP}},
        {"metadata_replay": {"archive_key": KEY, "observed_at": "2026-01-01"}},
    ],
)
def test_replay_endpoint_rejects_imprecise_body_without_io(
    replay_client, replay_env, monkeypatch, change
):
    from core import archive

    monkeypatch.setattr(archive, "get", lambda *args: pytest.fail("invalid body reached storage"))
    response = replay_client(
        {
            "metadata_replay": {"archive_key": KEY, "observed_at": STAMP},
            "dry_run": False,
            **change,
        }
    )
    assert response.status_code == 422 and response.json()["errors"]
    assert not replay_env[2].pushes and not replay_env[2].saves


def test_replay_endpoint_reports_missing_archive_and_pending_conflict(replay_client, replay_env):
    import gzip
    import json
    from core import archive

    hub = replay_env[2]
    del hub.objects[KEY]
    body = {"metadata_replay": {"archive_key": KEY, "observed_at": STAMP}, "dry_run": False}
    assert replay_client(body).status_code == 404
    hub.objects[archive.PENDING_KEY] = gzip.compress(
        json.dumps(
            {
                "planned": [],
                "operations": [],
                "writes": False,
            }
        ).encode()
    )
    response = replay_client(body)
    assert response.status_code == 409 and response.json()["errors"]
    assert not hub.pushes and not hub.saves


def test_capture_endpoint_blocks_pending_replay_before_spotify_setup(replay_client, replay_env):
    import gzip
    import json
    from core import archive

    hub = replay_env[2]
    hub.objects[archive.PENDING_KEY] = gzip.compress(
        json.dumps(
            {
                "intent": "metadata_replay",
                "planned": [],
                "operations": [],
                "writes": False,
            }
        ).encode()
    )
    response = replay_client({"title": "Title", "artist": "Artist"}, path="/capture")
    assert response.status_code == 200
    assert response.json()["ok"] is False and "recovery" in response.json()["message"]
    assert not hub.pushes and not hub.saves


def test_replay_endpoint_reports_catalog_read_failure_as_unavailable(
    replay_client, replay_env, monkeypatch
):
    from core.hub import HubError

    def fail(*args):
        raise HubError("hub unavailable")

    monkeypatch.setattr(replay_env[2], "pull", fail)
    response = replay_client({"metadata_replay": {"archive_key": KEY, "observed_at": STAMP}})
    assert response.status_code == 503 and response.json()["errors"]
    assert not replay_env[2].pushes and not replay_env[2].saves


def test_reconcile_response_exposes_quiet_review_items(settings, monkeypatch):
    from core import config, run
    from core.actions import RunLog

    reviews = [{"id": "curation-unlike:recording", "reason": "unliked_while_curated"}]
    monkeypatch.setattr(config, "Settings", lambda: settings)
    monkeypatch.setattr("core.workspaces.settings_for", lambda base, workspace: base)
    monkeypatch.setattr(
        run, "reconcile", lambda *args, **kw: RunLog(dry_run=True, review_items=reviews)
    )
    out = app._run(True)
    assert out["review_items"] == reviews
    assert "curation-unlike:recording" in out["summary"]
    assert not out["flags"]


def test_observation_import_and_preview_run_while_reconciliation_is_disabled(settings, monkeypatch):
    import gzip
    import json

    from core import actions, config, run

    calls = []

    def fake(s, **kwargs):
        calls.append(kwargs)
        return actions.RunLog(dry_run=kwargs["dry_run"], flags=["f"])

    monkeypatch.setattr(config, "Settings", lambda: settings)
    monkeypatch.setattr("core.archive.get", lambda *args: None)
    monkeypatch.setattr(run, "reconcile", fake)
    monkeypatch.delenv("RECONCILE_ENABLED", raising=False)
    worker = app.worker.get_raw_f()
    assert worker("observe", {}) == {"applied": {}, "flags": ["f"], "errors": []}
    receipt = json.loads(gzip.decompress(worker("preview", {"package": {"version": 1}})))
    assert receipt["dry_run"] is True
    assert calls[1] == {"dry_run": True, "package": {"version": 1}, "observation_key": None}
    assert calls[0]["writes"] is False and calls[0]["dry_run"] is False
    assert calls[0]["deadline"] > 0  # observation imports stop cleanly within budget


def test_package_requires_its_digest_and_clears_pending_after_the_run(settings, monkeypatch):
    import gzip
    import json

    from core import config, metadata, package

    store = {}
    monkeypatch.setattr(config, "Settings", lambda: settings)
    monkeypatch.setattr("core.archive.get", lambda s, k: store.get(k))
    monkeypatch.setattr("core.archive.put", lambda s, k, v: store.__setitem__(k, v))
    monkeypatch.setattr(metadata, "require_observed_contract", lambda hub: None)
    monkeypatch.setattr("core.spotify_client.SpotifyClient", lambda s: object())
    seen = []

    def apply(doc, spotify, hub, s, now, state, save):
        state["backup"] = "raw/b"
        save()
        seen.append(json.loads(gzip.decompress(store["music-sync/pending-reconcile.json.gz"])))
        return {"applied": True, "verified": True}

    monkeypatch.setattr(package, "apply", apply)
    worker = app.worker.get_raw_f()
    doc = {"version": 1, "likes": []}
    assert worker("package", {"package": doc, "confirm": "wrong"})["applied"] is False
    assert seen == []
    out = worker("package", {"package": doc, "confirm": package.digest(doc)})
    assert out == {"applied": True, "verified": True}
    assert seen[0]["intent"] == "package" and seen[0]["state"]["backup"] == "raw/b"
    assert json.loads(gzip.decompress(store["music-sync/pending-reconcile.json.gz"])) is None
    store["music-sync/pending-reconcile.json.gz"] = gzip.compress(
        json.dumps({"intent": "metadata_replay"}).encode()
    )
    blocked = worker("package", {"package": doc, "confirm": package.digest(doc)})
    assert blocked["applied"] is False and len(seen) == 1


def test_rate_limited_package_keeps_its_checkpoint_for_the_resumed_run(settings, monkeypatch):
    import gzip
    import json

    from core import config, metadata, package

    store = {}
    monkeypatch.setattr(config, "Settings", lambda: settings)
    monkeypatch.setattr("core.archive.get", lambda s, k: store.get(k))
    monkeypatch.setattr("core.archive.put", lambda s, k, v: store.__setitem__(k, v))
    monkeypatch.setattr(metadata, "require_observed_contract", lambda hub: None)
    monkeypatch.setattr("core.spotify_client.SpotifyClient", lambda s: object())

    def apply(doc, spotify, hub, s, now, state, save):
        state["backup"] = "raw/b"
        save()
        return {"applied": False, "rate_limited": {"retry_after": "70000"}, "remaining": {}}

    monkeypatch.setattr(package, "apply", apply)
    doc = {"version": 1, "likes": []}
    out = app.worker.get_raw_f()("package", {"package": doc, "confirm": package.digest(doc)})
    assert out["rate_limited"]["retry_after"] == "70000"
    kept = json.loads(gzip.decompress(store["music-sync/pending-reconcile.json.gz"]))
    assert kept["intent"] == "package" and kept["state"]["backup"] == "raw/b"


def test_stage_failure_before_a_run_log_is_reported_and_flagged(settings, monkeypatch):
    from core import config, run
    from core.hub import HubError

    def boom(*args, **kwargs):
        raise HubError("hub HTTP 503: private body")

    filed = []
    monkeypatch.setattr(config, "Settings", lambda: settings)
    monkeypatch.setattr("core.archive.get", lambda *args: None)
    monkeypatch.setattr(run, "reconcile", boom)
    monkeypatch.setattr(app, "_flag_quietly", lambda s, f, e: filed.append(e))
    monkeypatch.setenv("RECONCILE_ENABLED", "1")
    out = app.worker.get_raw_f()("reconcile", {"dry_run": False})
    assert out == {"errors": ["reconcile stopped before applying anything: HubError"]}
    assert filed == [out["errors"]] and "private" not in str(out)
    assert app.worker.get_raw_f()("reconcile", {"dry_run": True})["errors"]
    assert len(filed) == 1  # dry runs never file


@pytest.mark.parametrize(
    "pending,allowed",
    [
        ({"writes": False, "operations": [1]}, True),
        ({"writes": True, "operations": [1]}, False),
        ({"intent": "package", "digest": "d"}, False),
        ({"intent": "metadata_replay", "writes": False}, False),
    ],
)
def test_replan_discards_only_an_observation_only_import(settings, monkeypatch, pending, allowed):
    import gzip
    import json

    from core import actions, config, run

    key = "music-sync/pending-reconcile.json.gz"
    store = {key: gzip.compress(json.dumps(pending).encode())}
    monkeypatch.setattr(config, "Settings", lambda: settings)
    monkeypatch.setattr("core.archive.get", lambda s, k: store.get(k))
    monkeypatch.setattr("core.archive.put", lambda s, k, v: store.__setitem__(k, v))
    calls = []
    monkeypatch.setattr(run, "reconcile", lambda s, **kw: calls.append(kw) or actions.RunLog())
    out = app.worker.get_raw_f()("observe", {"replan": True})
    if allowed:
        assert json.loads(gzip.decompress(store[key])) is None and calls
    else:
        assert out["errors"] and not calls
        assert json.loads(gzip.decompress(store[key])) == pending
