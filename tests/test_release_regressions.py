"""User flows at the service boundaries; all state and transports are dummy fixtures."""

import copy
import gzip
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from core import actions, capture, run
from core.hub import Hub, HubError

NOW = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)
T = "2026-09-07T00:00:00.000Z"
A, B = "USAAA2600001", "USAAA2600002"


def raw(isrc=A, tid="a", added=T, playable=True):
    return {
        "added_at": added,
        "item": {
            "id": tid,
            "uri": f"spotify:track:{tid}",
            "name": "Song",
            "artists": [{"name": "Artist"}],
            "is_playable": playable,
            "external_ids": {"isrc": isrc},
        },
    }


class Store:
    def __init__(self, kind="curated", liked=1, member=True):
        self.tables = {
            "songs": {
                A: {
                    "id": A,
                    "liked": liked,
                    "liked_at": T if liked else None,
                    "first_seen": T,
                    "spotify_ids": ["a"],
                    "spotify_playable": 1,
                }
            },
            "playlists": {
                "P": {
                    "id": "P",
                    "name": "Playlist",
                    "kind": kind,
                    "pinned": 1,
                    "snapshot_id": "old",
                }
            },
            "playlist_songs": {},
            "provenance": {},
        }
        if member:
            self.tables["playlist_songs"][f"P:{A}"] = {
                "id": f"P:{A}",
                "playlist_id": "P",
                "isrc": A,
                "spotify_track_id": "a",
                "added_at": T,
                "deleted_at": None,
            }
        self.fail = None

    def pull(self, table, columns, since=""):
        return [{c: copy.deepcopy(r.get(c)) for c in columns} for r in self.tables[table].values()]

    def push(self, table, rows):
        if self.fail == table:
            self.fail = None
            raise HubError("injected hub failure")
        for row in rows:
            self.tables[table].setdefault(row["id"], {}).update(copy.deepcopy(row))
        return {"upserted": len(rows), "rejected": []}


class Spotify:
    def __init__(self, items=None, liked=True):
        self.items = copy.deepcopy([raw()] if items is None else items)
        self.liked = [raw()] if liked else []
        self.calls = []
        self.fail = None
        self.partial_add = False
        self.snapshot = 0

    def me(self):
        return {"id": "owner"}

    def get_playlists(self):
        return [
            {
                "id": "P",
                "name": "Playlist",
                "owner": {"id": "owner"},
                "snapshot_id": str(self.snapshot),
            }
        ]

    def get_playlist_items(self, pid, market):
        return copy.deepcopy(self.items)

    def get_liked(self, market):
        return copy.deepcopy(self.liked)

    def search_track(self, *args):
        return [raw()["item"]]

    def _call(self, kind, uris):
        self.calls.append((kind, list(uris)))
        if self.fail == kind:
            self.fail = None
            raise RuntimeError("injected Spotify failure")

    def like(self, uris):
        self._call("like", uris)
        self.liked = [raw()]

    def add_items(self, pid, uris):
        self._call("add", uris)
        for uri in uris:
            self.items.append(raw(tid=uri.split(":")[-1]))
            self.snapshot += 1
            if self.partial_add:
                self.partial_add = False
                raise RuntimeError("failed after first client chunk")

    def remove_items(self, pid, uris):
        self._call("remove", uris)
        self.items = [r for r in self.items if r["item"]["uri"] not in uris]
        self.snapshot += 1

    def set_description(self, pid, text):
        self._call("describe", [])

    def unfollow_playlist(self, pid):
        self._call("unfollow", [])
        self.items = []


@pytest.fixture
def archive_store(mocker):
    objects = {}

    def put(settings, key, data, **kwargs):
        objects[key] = bytes(data)

    mocker.patch("core.archive.put", side_effect=put)
    mocker.patch("core.archive.get", side_effect=lambda s, k, **kw: objects.get(k), create=True)
    mocker.patch("core.run.flags.file")
    return objects


def execute(settings, sp, hub, **kw):
    return run.reconcile(settings, spotify=sp, hub=hub, now=NOW, **kw)


def test_mixed_hub_patches_preserve_identity_and_explicit_null():
    store = {
        f"P:{A}": {
            "id": f"P:{A}",
            "playlist_id": "P",
            "isrc": A,
            "spotify_track_id": "a",
            "added_at": T,
            "deleted_at": None,
        }
    }
    bodies = []

    def handler(req):
        body = json.loads(req.content)
        bodies.append(body)
        # The live worker rejects missing/null updated_at before catalog validation.
        rejected = [
            {"id": row["id"], "col": "updated_at", "rule": "required"}
            for row in body["rows"]
            if row.get("updated_at") is None
        ]
        if rejected:
            return httpx.Response(200, json={"upserted": 0, "rejected": rejected})
        for row in body["rows"]:
            # Model the hub's partial-column update contract.
            store.setdefault(row["id"], {}).update({c: row.get(c) for c in body["columns"]})
        return httpx.Response(200, json={"upserted": len(body["rows"]), "rejected": []})

    hub = Hub("https://hub.test", "dummy", httpx.Client(transport=httpx.MockTransport(handler)))
    hub.push(
        "playlist_songs",
        [
            {"id": f"P:{A}", "deleted_at": T},
            {
                "id": f"P:{B}",
                "playlist_id": "P",
                "isrc": B,
                "spotify_track_id": "b",
                "added_at": T,
                "deleted_at": None,
            },
        ],
    )
    assert store[f"P:{A}"]["isrc"] == A
    assert store[f"P:{A}"]["added_at"] == T
    assert store[f"P:{B}"]["deleted_at"] is None
    assert len(bodies) == 2


@pytest.mark.parametrize("kind", ["curated", "inbox"])
def test_import_observes_without_inventing_likes_or_fifo(settings, archive_store, kind):
    hub = Store(kind=kind, liked=0, member=False)
    sp = Spotify([raw(), raw(B, "b")], liked=False)
    settings.inbox_cap = 1
    for _ in range(2):
        out = execute(settings, sp, hub, writes=False)
        assert not out.errors
        assert hub.tables["songs"][A]["liked"] == 0
        assert all(not r.get("deleted_at") for r in hub.tables["playlist_songs"].values())
        assert len(hub.tables["playlist_songs"]) == 2
        assert sp.calls == []


@pytest.mark.parametrize("gesture", ["unheart", "autolike", "undo", "relink", "same_uri"])
def test_failed_mutation_preserves_baseline_and_recovers_next_run(settings, archive_store, gesture):
    hub, sp = Store(), Spotify()
    if gesture == "unheart":
        sp.liked, sp.fail = [], "remove"
    elif gesture == "autolike":
        hub = Store(liked=0, member=False)
        sp.liked, sp.fail = [], "like"
    elif gesture == "undo":
        hub.tables["songs"][A]["liked"] = 0
        hub.tables["playlist_songs"][f"P:{A}"]["deleted_at"] = T
        sp.items, sp.fail = [], "add"
    elif gesture == "relink":
        hub.tables["songs"][A]["spotify_ids"] = ["new", "a"]
        sp.items, sp.fail = [raw(playable=False)], "add"
    else:
        sp.items, sp.fail = [raw(), raw()], "add"
    baseline = copy.deepcopy(hub.tables)
    out = execute(settings, sp, hub)
    assert out.errors
    assert hub.tables == baseline
    if gesture == "relink":
        assert len(sp.items) == 1 and sp.items[0]["item"]["id"] == "a"
    out = execute(settings, sp, hub)
    assert not out.errors
    if gesture == "unheart":
        assert sp.items == []
        row = hub.tables["playlist_songs"][f"P:{A}"]
        assert row["deleted_at"] and row["added_at"] == T and row["isrc"] == A
        # The user's 7-day undo must survive a failed removal and its retry.
        sp.liked = [raw()]
        assert not execute(settings, sp, hub).errors
        assert len(sp.items) == 1
        assert hub.tables["playlist_songs"][f"P:{A}"]["deleted_at"] is None
    else:
        assert len(sp.items) == 1
        assert hub.tables["songs"][A]["liked"] == 1
        assert not hub.tables["playlist_songs"][f"P:{A}"].get("deleted_at")
    assert not execute(settings, sp, hub).errors
    assert len(sp.items) == 1


@pytest.mark.parametrize("liked,count", [(False, 2), (True, 3)])
def test_dedupe_obeys_final_membership(settings, archive_store, liked, count):
    sp, hub = Spotify([raw()] * count, liked=liked), Store()
    assert not execute(settings, sp, hub).errors
    assert len(sp.items) == (1 if liked else 0)
    assert not execute(settings, sp, hub).errors
    assert len(sp.items) == (1 if liked else 0)


def test_new_owned_playlist_likes_initial_add_once(settings, archive_store):
    hub, sp = Store(liked=0, member=False), Spotify(liked=False)
    hub.tables["playlists"].clear()
    assert not execute(settings, sp, hub).errors
    assert sp.liked
    assert not execute(settings, sp, hub).errors
    assert [c[0] for c in sp.calls].count("like") == 1


def test_dry_run_exposes_reviewable_removal(settings, archive_store):
    sp, hub = Spotify(liked=False), Store()
    out = execute(settings, sp, hub, dry_run=True)
    assert sp.calls == [] and archive_store == {}
    details = getattr(out, "planned", [])
    removal = next((a for a in details if a["kind"] == "remove_item"), None)
    assert removal and removal["playlist_id"] == "P" and removal["isrc"] == A
    assert removal["playlist_name"] == "Playlist" and removal["title"] == "Song"
    assert removal["reason"] == "un-hearted"
    assert out.applied == {}
    assert A in out.summary() and "Playlist" in out.summary()


def test_capture_archives_before_add_and_failure_prevents_writes(settings, archive_store, mocker):
    sp, hub = Spotify(items=[], liked=False), Store(kind="inbox", member=False)

    def fail(*args):
        assert sp.calls == []
        raise RuntimeError("archive offline")

    mocker.patch("core.archive.put", side_effect=fail)
    with pytest.raises(RuntimeError, match="archive offline"):
        capture.capture({"title": "Song", "artist": "Artist"}, sp, hub, settings, NOW)
    assert sp.calls == []


def test_capture_provenance_retry_uses_live_presence(settings, archive_store):
    sp, hub = Spotify(items=[], liked=False), Store(kind="inbox", member=False)
    hub.fail = "provenance"
    payload = {"title": "Song", "artist": "Artist", "apple_music_id": "dummy"}
    with pytest.raises(HubError):
        capture.capture(payload, sp, hub, settings, NOW)
    out = capture.capture(payload, sp, hub, settings, NOW)
    assert out["ok"] and len(sp.items) == 1
    assert hub.tables["provenance"][f"shazam:dummy:{A}"]["from_kind"] == "shazam"
    backups = [gzip.decompress(v) for k, v in archive_store.items() if "capture" in k]
    assert backups


def test_activation_manifest_roundtrip_default_off():
    from scripts import provision, sync_secrets

    manifest = Path(".env.tpl").read_text()
    ref = "op://Music Sync/Music Sync ENV/RECONCILE_ENABLED"
    assert f"RECONCILE_ENABLED={ref}" in manifest
    assert "RECONCILE_ENABLED" in provision.FIELDS
    for value in ["0", "1"]:
        assert sync_secrets.parse_dotenv(manifest.replace(ref, value))["RECONCILE_ENABLED"] == value


def test_empty_manual_request_cannot_mutate_disabled(monkeypatch):
    import app

    monkeypatch.delenv("RECONCILE_ENABLED", raising=False)
    called = []
    monkeypatch.setattr(app, "_run", lambda **kw: called.append(kw))
    # Route remote dispatch locally, without contacting Modal.
    if hasattr(app, "worker"):
        monkeypatch.setattr(app.worker, "remote", app.worker.get_raw_f())
    assert app.reconcile.get_raw_f()({}) == {"skipped": True}
    assert called == []


def test_partial_client_add_and_hub_failure_recover_without_duplicate(settings, archive_store):
    hub, sp = Store(kind="smart"), Spotify(items=[])
    hub.tables["playlists"]["P"]["rule"] = {"v": 1}
    hub.tables["songs"][B] = {**hub.tables["songs"][A], "id": B, "spotify_ids": ["b"]}
    sp.liked.append(raw(B, "b"))
    sp.partial_add = True
    assert execute(settings, sp, hub).errors
    assert len(sp.items) == 1
    hub.fail = "playlist_songs"
    assert execute(settings, sp, hub).errors
    assert len(sp.items) == 2
    assert not execute(settings, sp, hub).errors
    assert sorted(r["item"]["id"] for r in sp.items) == ["a", "b"]
    assert len(hub.tables["playlist_songs"]) == 2


def test_checkpoint_failure_after_remove_leaves_durable_repair(settings, archive_store, mocker):
    from core import archive

    hub, sp = Store(), Spotify([raw(), raw()])
    original = archive.put
    failed = False

    def put(settings, key, data):
        nonlocal failed
        if key == archive.PENDING_KEY and not sp.items and not failed:
            failed = True
            raise RuntimeError("checkpoint outage after destructive remove")
        return original(settings, key, data)

    mocker.patch("core.archive.put", side_effect=put)
    assert execute(settings, sp, hub).errors
    assert sp.items == []
    assert not execute(settings, sp, hub).errors
    assert len(sp.items) == 1


def test_pending_archive_failure_prevents_first_mutation(settings, archive_store, mocker):
    from core import archive

    hub, sp = Store(), Spotify(liked=False)
    original = archive.put

    def put(settings, key, data):
        if key == archive.PENDING_KEY:
            raise RuntimeError("pending storage outage")
        return original(settings, key, data)

    mocker.patch("core.archive.put", side_effect=put)
    assert execute(settings, sp, hub).errors
    assert sp.calls == []


def test_incomplete_repair_blocks_capture_and_observation(settings, archive_store):
    hub, sp = Store(), Spotify([raw(), raw()])
    sp.fail = "add"
    assert execute(settings, sp, hub).errors
    hub.tables["playlists"]["P"]["kind"] = "inbox"
    before = copy.deepcopy(hub.tables), copy.deepcopy(sp.calls)
    assert execute(settings, sp, hub, writes=False).errors
    out = capture.capture({"title": "Song", "artist": "Artist"}, sp, hub, settings, NOW)
    assert not out["ok"] and "recovery" in out["message"]
    assert before == (hub.tables, sp.calls)


@pytest.mark.parametrize("expired", [False, True])
def test_duplicate_smart_exclusion_and_expiry_never_readd(settings, archive_store, expired):
    hub, sp = Store(kind="smart"), Spotify([raw(), raw(), raw()])
    p = hub.tables["playlists"]["P"]
    p["rule"] = {"v": 1, "first_year": {"lt": 1900}}
    if expired:
        p.update(pinned=0, expires_at=T)
    assert not execute(settings, sp, hub).errors
    assert sp.items == []
    assert not any(kind == "add" for kind, _ in sp.calls)


def test_capture_raw_archive_precedes_add_and_trim(settings, archive_store, mocker):
    from core import archive

    sp, hub = Spotify([raw(B, "b")], liked=False), Store(kind="inbox", member=False)
    settings.inbox_cap = 1
    before = copy.deepcopy(sp.items)
    original = archive.put

    def put(settings, key, data):
        assert sp.calls == []
        assert json.loads(gzip.decompress(data))["items"] == before
        return original(settings, key, data)

    mocker.patch("core.archive.put", side_effect=put)
    assert capture.capture({"title": "Song", "artist": "Artist"}, sp, hub, settings, NOW)["ok"]
    assert [c[0] for c in sp.calls] == ["add", "remove"]
    assert sp.items[0]["item"]["id"] == "a"
    assert any(key.startswith("raw/spotify-capture/") for key in archive_store)


def test_all_entrypoints_share_one_serial_modal_worker(monkeypatch):
    import app
    from concurrent.futures import ThreadPoolExecutor
    from threading import Lock

    assert hasattr(app, "worker"), "all callers need one shared worker"
    # Capture SDK decorator arguments by importing against a local recording App.
    # Runtime dispatch below uses this same declared concurrency contract.
    source = Path("app.py").read_text()
    import ast

    tree = ast.parse(source)
    worker_node = next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "worker"
    )
    keywords = {
        k.arg: ast.literal_eval(k.value)
        for d in worker_node.decorator_list
        if isinstance(d, ast.Call)
        for k in d.keywords
        if k.arg in {"max_containers", "max_inputs"}
    }
    assert keywords == {"max_containers": 1, "max_inputs": 1}
    gate = Lock()
    calls = []

    def remote(operation, body=None):
        with gate:
            calls.append(operation)
            return app.worker.get_raw_f()(operation, body)

    monkeypatch.setattr(app.worker, "remote", remote)
    monkeypatch.setenv("RECONCILE_ENABLED", "1")
    monkeypatch.setattr(app, "_run", lambda **kw: {"reconciled": True})
    monkeypatch.setattr(app, "_capture", lambda body: {"captured": body})
    inputs = [
        (app.reconcile_cron.get_raw_f(), ()),
        (app.reconcile.get_raw_f(), ({},)),
        (app.capture.get_raw_f(), ({"id": 1},)),
        (app.capture.get_raw_f(), ({"id": 2},)),
    ]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda pair: pair[0](*pair[1]), inputs))
    assert calls.count("reconcile") == 2 and calls.count("capture") == 2
    assert results == [
        {"reconciled": True},
        {"reconciled": True},
        {"captured": {"id": 1}},
        {"captured": {"id": 2}},
    ]


def test_disabled_worker_allows_explicit_dry_run_and_capture(monkeypatch):
    import app

    assert hasattr(app, "worker")
    monkeypatch.delenv("RECONCILE_ENABLED", raising=False)
    monkeypatch.setattr(app, "_run", lambda **kw: kw)
    monkeypatch.setattr(app, "_capture", lambda body: {"ok": True})
    worker = app.worker.get_raw_f()
    assert worker("reconcile", {"dry_run": True}) == {"dry_run": True}
    assert worker("reconcile", {"dry_run": "false"}) == {"skipped": True}
    assert worker("capture", {}) == {"ok": True}


def test_activation_provision_outputs_zero_without_services(capsys, monkeypatch):
    from scripts import provision

    monkeypatch.setattr("sys.argv", ["provision.py", "--field", "RECONCILE_ENABLED"])
    provision.main()
    assert capsys.readouterr().out == "0\n"


def test_conflicting_new_membership_unheart_retains_full_undo_row(settings, archive_store):
    hub, sp = Store(member=False), Spotify(liked=False)
    assert not execute(settings, sp, hub).errors
    row = hub.tables["playlist_songs"][f"P:{A}"]
    assert row.get("isrc") == A and row.get("playlist_id") == "P"
    assert row["added_at"] == T and row["spotify_track_id"] == "a" and row["deleted_at"]
    sp.liked = [raw()]
    assert not execute(settings, sp, hub).errors
    assert len(sp.items) == 1


def test_recovery_archives_fresh_raw_before_replay(settings, archive_store, mocker):
    from core import archive

    hub, sp = Store(), Spotify([raw(), raw()])
    sp.fail = "add"
    assert execute(settings, sp, hub).errors
    sp.calls.clear()
    backups_before = {k for k in archive_store if k.startswith("raw/")}
    original = archive.put

    def put(settings, key, data):
        if key.startswith("raw/"):
            assert not sp.calls
            assert json.loads(gzip.decompress(data))["items"]["P"] == []
        return original(settings, key, data)

    mocker.patch("core.archive.put", side_effect=put)
    assert not execute(settings, sp, hub).errors
    assert len({k for k in archive_store if k.startswith("raw/")} - backups_before) == 1


def test_real_spotify_client_chunk_failure_replays_only_missing_uris(settings):
    from core.model import Action
    from core.spotify_client import SpotifyClient

    uris = [f"spotify:track:dummy{i}" for i in range(101)]
    present, posts, pending = [], [], []

    def handler(req):
        if req.url.path == "/api/token":
            return httpx.Response(200, json={"access_token": "dummy"})
        if req.method == "GET":
            return httpx.Response(
                200, json={"items": [{"item": {"uri": uri}} for uri in present], "next": None}
            )
        chunk = json.loads(req.content)["uris"]
        posts.append(chunk)
        if len(posts) == 2:
            return httpx.Response(500, json={"error": "second chunk failed"})
        present.extend(chunk)
        return httpx.Response(201, json={"snapshot_id": "dummy"})

    client = SpotifyClient(settings, httpx.Client(transport=httpx.MockTransport(handler)))
    plan = [Action("add_item", playlist_id="P", uri=uri) for uri in uris]

    def checkpoint(remaining):
        pending[:] = copy.deepcopy(remaining)

    out = actions.apply(
        plan, client, Store(), False, checkpoint=checkpoint, market=settings.spotify_market
    )
    assert out.errors and len(present) == 100 and pending
    out = actions.apply(
        plan,
        client,
        Store(),
        False,
        checkpoint=checkpoint,
        pending=copy.deepcopy(pending),
        market=settings.spotify_market,
    )
    assert not out.errors and present == uris and pending == []
    assert [len(p) for p in posts] == [100, 1, 1]


def test_provision_requests_read_and_write_for_recovery(mocker):
    from scripts import provision

    bodies = []

    def handler(req):
        if req.url.path.endswith("permission_groups"):
            return httpx.Response(
                200,
                json={
                    "result": [
                        {"name": "Workers R2 Storage Bucket Item Read", "id": "read"},
                        {"name": "Workers R2 Storage Bucket Item Write", "id": "write"},
                    ]
                },
            )
        if req.method == "GET":
            return httpx.Response(200, json={"result": []})
        bodies.append(json.loads(req.content))
        return httpx.Response(200, json={"result": {"value": "dummy"}})

    client = httpx.Client(base_url="https://cf.test", transport=httpx.MockTransport(handler))
    mocker.patch.object(provision, "op_read", return_value="dummy")
    mocker.patch.object(provision.httpx, "Client", return_value=client)
    assert provision.mint_r2_token() == "dummy"
    assert bodies[0]["policies"][0]["permission_groups"] == [{"id": "read"}, {"id": "write"}]


def test_failed_observation_import_can_resume_without_enabling_writes(settings, archive_store):
    sp, hub = Spotify(liked=False), Store(liked=0, member=False)
    hub.fail = "playlist_songs"
    assert execute(settings, sp, hub, writes=False).errors
    assert not execute(settings, sp, hub, writes=False).errors
    assert sp.calls == []
    assert hub.tables["songs"][A]["liked"] == 0
    assert hub.tables["playlist_songs"][f"P:{A}"]["isrc"] == A


def test_observation_import_meets_hub_datetime_contract_and_recovers(settings, archive_store):
    import re

    store, sp = Store(member=False), Spotify()
    store.tables["songs"].clear()
    store.fail = "playlist_songs"
    sp.items = [raw(added="2026-09-07T00:00:00Z")]
    sp.liked = [raw(added="2026-09-07T00:00:00+00:00")]

    def handler(req):
        body = json.loads(req.content)
        table = body["table"]
        if req.url.path.endswith("/pull"):
            return httpx.Response(200, json={"rows": store.pull(table, body["columns"])})
        # Match the worker's required stamp and catalog datetime validation.
        for row in body["rows"]:
            for col in ("updated_at", "first_seen", "liked_at", "added_at"):
                value = row.get(col)
                if (col == "updated_at" or value is not None) and not re.fullmatch(
                    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", value or ""
                ):
                    return httpx.Response(
                        200,
                        json={
                            "upserted": 0,
                            "rejected": [{"id": row["id"], "col": col, "rule": "type"}],
                        },
                    )
        try:
            return httpx.Response(200, json=store.push(table, body["rows"]))
        except HubError as exc:
            return httpx.Response(500, text=str(exc))

    hub = Hub("https://hub.test", "dummy", httpx.Client(transport=httpx.MockTransport(handler)))
    out = execute(settings, sp, hub, writes=False)
    assert len(out.errors) == 1 and "injected hub failure" in out.errors[0]
    pending = json.loads(gzip.decompress(archive_store[run.archive.PENDING_KEY]))
    # Saved operations retain the original value; normalization is a wire concern.
    assert pending["operations"][0]["rows"][0]["added_at"] == "2026-09-07T00:00:00Z"
    assert not execute(settings, sp, hub, writes=False).errors
    assert json.loads(gzip.decompress(archive_store[run.archive.PENDING_KEY])) is None
    assert sp.calls == []
    assert store.tables["songs"][A]["first_seen"] == "2026-09-08T12:00:00.000Z"
    assert store.tables["songs"][A]["liked_at"] == T
    assert store.tables["playlist_songs"][f"P:{A}"]["added_at"] == T


def test_pending_observation_blocks_activation_preview_until_recovered(
    settings, archive_store, mocker
):
    sp, hub = Spotify(liked=False), Store(kind="smart", liked=0, member=False)
    hub.tables["playlists"]["P"]["rule"] = {"v": 1}
    hub.fail = "playlist_songs"
    assert execute(settings, sp, hub, writes=False).errors
    pending = archive_store[run.archive.PENDING_KEY]
    assert json.loads(gzip.decompress(pending))["operations"]

    push = mocker.spy(hub, "push")
    put = run.archive.put
    filed = run.flags.file
    put.reset_mock()
    filed.reset_mock()
    before = copy.deepcopy((hub.tables, vars(sp), archive_store))
    for writes in (True, False):
        out = execute(settings, sp, hub, dry_run=True, writes=writes)
        assert out.errors == [
            "Activation preview blocked by pending recovery; "
            "complete recovery, then request a fresh dry run"
        ]
        assert out.dry_run and not out.applied and not out.planned
        assert (hub.tables, vars(sp), archive_store) == before
        assert archive_store[run.archive.PENDING_KEY] == pending
        push.assert_not_called()
        put.assert_not_called()
        filed.assert_not_called()

    assert not execute(settings, sp, hub, writes=False).errors
    assert json.loads(gzip.decompress(archive_store[run.archive.PENDING_KEY])) is None
    assert sp.calls == []
    out = execute(settings, sp, hub, dry_run=True)
    assert not out.errors
    assert any(
        a["kind"] == "remove_item"
        and a["playlist_id"] == "P"
        and a["isrc"] == A
        and a["reason"] == "removed by rule"
        for a in out.planned
    )
    assert sp.calls == [] and not out.applied
