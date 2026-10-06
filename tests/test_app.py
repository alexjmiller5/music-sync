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
    from fastapi import FastAPI
    from core import archive, config

    objects = {}
    monkeypatch.setattr(config, "Settings", lambda: settings)
    monkeypatch.setattr(archive, "get", lambda settings, key: objects.get(key))
    monkeypatch.setattr(
        archive, "put", lambda settings, key, value: objects.__setitem__(key, value)
    )
    monkeypatch.setattr(app.worker, "remote", app.worker.get_raw_f())
    api = FastAPI()
    api.post("/capture-consumer")(app.capture_consumer.get_raw_f())
    api.post("/capture-access")(app.capture_access.get_raw_f())

    def post(path, body, token=None):
        async def request():
            headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=api), base_url="http://test"
            ) as client:
                return await client.post(path, json=body, headers=headers)

        return asyncio.run(request())

    return post, objects


def issue_capture_token(post):
    response = post("/capture-access", {"action": "issue", "label": "phone"})
    assert response.status_code == 200
    return response.json()


def test_consumer_capture_requires_valid_bearer_token(capture_api):
    post, _ = capture_api
    assert post("/capture-consumer", CONSUMER_BODY).status_code == 401
    assert post("/capture-consumer", CONSUMER_BODY, "invalid").status_code == 401


def test_operator_can_issue_and_revoke_one_consumer_without_affecting_another(
    capture_api, monkeypatch
):
    from core import capture as capture_mod

    post, _ = capture_api
    monkeypatch.setattr(capture_mod, "resolve_track", lambda *args: {"id": "selected"})
    monkeypatch.setattr(
        app,
        "_capture",
        lambda body, selected=None, settings=None, record=None: {
            "ok": True,
            "message": "added",
            "isrc": "USAAA2600001",
        },
    )
    first = issue_capture_token(post)
    second = issue_capture_token(post)

    response = post("/capture-consumer", CONSUMER_BODY, first["token"])
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "capture_id": CAPTURE_ID,
        "isrc": "USAAA2600001",
        "spotify_outcome": "added",
    }
    revoked = post("/capture-access", {"action": "revoke", "client_id": first["client_id"]})
    assert revoked.status_code == 200 and revoked.json() == {"ok": True, "revoked": True}
    assert (
        post(
            "/capture-consumer",
            {**CONSUMER_BODY, "capture_id": "a4af8b79-a4c9-4b7a-a616-553021037845"},
            first["token"],
        ).status_code
        == 401
    )
    assert (
        post(
            "/capture-consumer",
            {**CONSUMER_BODY, "capture_id": "a4af8b79-a4c9-4b7a-a616-553021037845"},
            second["token"],
        ).status_code
        == 200
    )


def test_consumer_capture_rejects_malformed_body_and_changed_replay(capture_api, monkeypatch):
    from core import capture as capture_mod

    post, _ = capture_api
    token = issue_capture_token(post)["token"]
    monkeypatch.setattr(capture_mod, "resolve_track", lambda *args: {"id": "selected"})
    monkeypatch.setattr(
        app,
        "_capture",
        lambda body, selected=None, settings=None, record=None: {
            "ok": True,
            "message": "added",
            "isrc": "USAAA2600001",
        },
    )

    malformed = post("/capture-consumer", {**CONSUMER_BODY, "capture_id": "bad"}, token)
    assert malformed.status_code == 422 and malformed.json()["ok"] is False
    assert post("/capture-consumer", CONSUMER_BODY, token).status_code == 200
    conflict = post("/capture-consumer", {**CONSUMER_BODY, "title": "Other"}, token)
    assert conflict.status_code == 409 and conflict.json()["ok"] is False


def test_consumer_capture_replays_receipt_without_recapturing(capture_api, monkeypatch):
    from core import capture as capture_mod

    post, _ = capture_api
    token = issue_capture_token(post)["token"]
    calls = []
    monkeypatch.setattr(capture_mod, "resolve_track", lambda *args: {"id": "selected"})

    def perform(body, selected=None, settings=None, record=None):
        calls.append(body)
        return {"ok": True, "message": "added", "isrc": "USAAA2600001"}

    monkeypatch.setattr(app, "_capture", perform)
    assert post("/capture-consumer", CONSUMER_BODY, token).status_code == 200
    replay = post("/capture-consumer", CONSUMER_BODY, token)
    assert replay.status_code == 200 and replay.json()["capture_id"] == CAPTURE_ID
    assert len(calls) == 1


def test_consumer_capture_returns_safe_unavailable_when_receipt_write_fails(
    capture_api, monkeypatch
):
    from core import capture_clients

    post, objects = capture_api
    token = issue_capture_token(post)["token"]
    monkeypatch.setattr(
        app, "_capture", lambda body: {"ok": True, "message": "added", "isrc": "USAAA2600001"}
    )

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


def test_consumer_capture_relays_spotify_rate_limit_as_retry_after(capture_api, monkeypatch):
    import httpx
    from core import capture as cap

    post, _ = capture_api
    token = issue_capture_token(post)["token"]

    def rate_limited(payload, spotify, settings):
        response = httpx.Response(
            429,
            headers={"Retry-After": "120"},
            request=httpx.Request("GET", "https://api.spotify.com/v1/search"),
        )
        raise httpx.HTTPStatusError("429", request=response.request, response=response)

    monkeypatch.setattr(cap, "resolve_track", rate_limited)
    response = post("/capture-consumer", CONSUMER_BODY, token)
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "120"
    assert response.json() == {
        "ok": False,
        "message": "Spotify is rate limiting; retry later",
        "capture_id": CAPTURE_ID,
        "spotify_outcome": "not_added",
    }


def test_queued_consumer_rechecks_revocation_inside_worker(capture_api, settings, monkeypatch):
    from core import capture_clients

    post, _ = capture_api
    issued = issue_capture_token(post)
    captures = []
    monkeypatch.setattr(
        app,
        "_capture",
        lambda body: (
            captures.append(body) or {"ok": True, "message": "added", "isrc": "USAAA2600001"}
        ),
    )
    raw_worker = app.worker.get_raw_f()

    def revoke_before_worker(operation, body=None):
        if operation == "consumer_capture":
            capture_clients.revoke(settings, body["client_id"])
        return raw_worker(operation, body)

    monkeypatch.setattr(app.worker, "remote", revoke_before_worker)
    response = post("/capture-consumer", CONSUMER_BODY, issued["token"])

    assert response.status_code == 401
    assert captures == []


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
    first_response = post("/capture-consumer", CONSUMER_BODY, token)
    assert first_response.status_code == 503
    state_key = next(key for key in objects if key.startswith(capture_clients.RECEIPTS_PREFIX))
    selected_state = json.loads(gzip.decompress(objects[state_key]))
    assert selected_state["selected_track"]["id"] == "a" and "isrc" not in selected_state
    spotify.tracks = [second]

    retry = post("/capture-consumer", CONSUMER_BODY, token)

    assert retry.status_code == 200
    assert retry.json() == {
        "ok": True,
        "capture_id": CAPTURE_ID,
        "isrc": "USAAA2600001",
        "spotify_outcome": "added",
    }
    assert [call for call in spotify.calls if call[0] == "add"] == [
        ("add", "IN", ["spotify:track:a"])
    ]


def test_incomplete_selection_is_not_pinned_and_later_retry_can_resolve(capture_api, monkeypatch):
    from core import capture_clients, hub, spotify_client
    from tests.test_capture import FakeHub, FakeSpotify, INBOX, track

    post, objects = capture_api
    token = issue_capture_token(post)["token"]
    spotify = FakeSpotify([track("a", "Song", "Artist", isrc=None)])
    fake_hub = FakeHub(INBOX)
    monkeypatch.setattr(spotify_client, "SpotifyClient", lambda settings: spotify)
    monkeypatch.setattr(hub, "Hub", lambda *args: fake_hub)

    first = post("/capture-consumer", CONSUMER_BODY, token)

    assert first.status_code == 422
    assert not any(key.startswith(capture_clients.RECEIPTS_PREFIX) for key in objects)
    assert spotify.calls == [] and fake_hub.pushed == []

    spotify.tracks = [track("a", "Song", "Artist", isrc="USAAA2600001")]
    retry = post("/capture-consumer", CONSUMER_BODY, token)

    assert retry.status_code == 200
    assert retry.json() == {
        "ok": True,
        "capture_id": CAPTURE_ID,
        "isrc": "USAAA2600001",
        "spotify_outcome": "added",
    }
    assert spotify.searches == [
        ("metadata", "Song", "Artist", "US"),
        ("metadata", "Song", "Artist", "US"),
    ]
    assert [call for call in spotify.calls if call[0] == "add"] == [
        ("add", "IN", ["spotify:track:a"])
    ]


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


@pytest.mark.parametrize(
    "failure,want",
    [
        ("before_add", "not_added"),
        ("add_timeout", "unknown"),
        ("after_add", "added"),
        (None, "added"),
    ],
)
def test_capture_http_outcome_tracks_spotify_effect_even_when_catalog_fails(
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
    response = post("/capture-consumer", CONSUMER_BODY, token)
    assert response.json()["capture_id"] == CAPTURE_ID
    assert response.json()["spotify_outcome"] == want
    assert response.status_code == (200 if failure is None else 503)
    assert bool(spotify.calls) == (failure != "before_add")

    if failure == "after_add":
        # Retry fails earlier this time. It must retain proof of the earlier add.
        def broken_catalog_again():
            raise RuntimeError("catalog unavailable")

        catalog.catalog = broken_catalog_again
        retry = post("/capture-consumer", CONSUMER_BODY, token)
        assert retry.json()["spotify_outcome"] == "added"
        assert len(spotify.calls) == 1


@pytest.mark.parametrize("legacy_outcome", [None, "unknown", "added"])
def test_retry_cannot_report_not_added_after_a_prior_possible_add(
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
    response = post("/capture-consumer", CONSUMER_BODY, client["token"])
    assert response.json()["spotify_outcome"] == (legacy_outcome or "unknown")
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
    response = post("/capture-consumer", CONSUMER_BODY, token)
    assert response.status_code == 503
    assert response.json()["spotify_outcome"] == "unknown"
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
    response = post("/capture-consumer", CONSUMER_BODY, token)
    assert response.status_code == 503
    assert response.json()["spotify_outcome"] == "added"
    assert spotify.calls == []


def test_acknowledged_add_stays_added_when_outcome_receipt_write_fails(capture_api, monkeypatch):
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
            if json.loads(gzip.decompress(value)).get("spotify_outcome") == "added":
                raise RuntimeError("receipt unavailable")
        objects[key] = value

    monkeypatch.setattr(archive, "put", put)
    response = post("/capture-consumer", CONSUMER_BODY, token)
    assert response.status_code == 503
    assert response.json()["spotify_outcome"] == "added"
    assert len(spotify.calls) == 1


def test_post_add_rate_limit_retains_added_and_retry_after(capture_api, monkeypatch):
    import httpx
    from core import hub, spotify_client
    from tests.test_capture import FakeHub, FakeSpotify, INBOX, track

    post, _ = capture_api
    token = issue_capture_token(post)["token"]
    spotify = FakeSpotify([track("track", "Song", "Artist")])
    catalog = FakeHub(INBOX)

    def push(*args):
        response = httpx.Response(
            429, headers={"Retry-After": "60"}, request=httpx.Request("POST", "https://hub.test")
        )
        raise httpx.HTTPStatusError("limited", request=response.request, response=response)

    catalog.push = push
    monkeypatch.setattr(spotify_client, "SpotifyClient", lambda *args: spotify)
    monkeypatch.setattr(hub, "Hub", lambda *args: catalog)
    response = post("/capture-consumer", CONSUMER_BODY, token)
    assert response.json()["spotify_outcome"] == "added"
    assert response.headers["Retry-After"] == "60"
