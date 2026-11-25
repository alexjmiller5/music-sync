import gzip
import json
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from core import quick_likes
from tests.test_capture import FakeHub, FakeSpotify, INBOX, track

NOW = datetime(2030, 1, 2, tzinfo=timezone.utc)


def item(tid="a", stamp="2030-01-01T00:00:00Z"):
    return {"added_at": stamp, "track": track(tid, "Song", "Artist", "USAAA2600001")}


def page(*items):
    return {"items": list(items), "total": len(items), "offset": 0, "next": None}


def test_baseline_no_replay_and_partial_absence_never_unlikes():
    baseline, changes = quick_likes.diff(None, page(item("a"), item("b")))
    assert changes == []
    after, changes = quick_likes.diff(baseline, page(item("c", "2030-01-02T00:00:00Z"), item("a")))
    assert [r["track"]["id"] for r in changes] == ["c"]
    assert quick_likes.diff(after, page(item("c", "2030-01-02T00:00:00Z"), item("a")))[1] == []


def test_resave_is_a_new_positive_event_and_ties_are_valid():
    baseline, _ = quick_likes.diff(None, page(item("a"), item("b")))
    _, changes = quick_likes.diff(baseline, page(item("a", "2030-01-02T00:00:00Z"), item("b")))
    assert [r["track"]["id"] for r in changes] == ["a"]


@pytest.mark.parametrize(
    "body",
    [
        page(item("a"), item("a")),
        page(item("a"), item("b", "2030-01-02T00:00:00Z")),
        page(item("a", "2030-01-01T00:00:00")),
        {"items": []},
        page(*(item(str(i)) for i in range(51))),
    ],
)
def test_bad_pages_fail_closed(body):
    with pytest.raises(ValueError):
        quick_likes.diff(None, body)


def test_no_overlap_waits_for_full_sync():
    baseline, _ = quick_likes.diff(None, page(item("a")))
    with pytest.raises(ValueError, match="overlap"):
        quick_likes.diff(baseline, page(item("b")))


@pytest.fixture
def store(monkeypatch):
    from core import archive

    objects = {}
    monkeypatch.setattr(archive, "get", lambda s, k: objects.get(k))
    monkeypatch.setattr(archive, "put", lambda s, k, v: objects.__setitem__(k, v))
    return objects


class RecentSpotify(FakeSpotify):
    def __init__(self, body):
        super().__init__([row["track"] for row in body["items"]])
        self.body = body
        self.reads = 0

    def get_recent_likes(self, market):
        self.reads += 1
        return deepcopy(self.body)


def test_disabled_cooldown_and_dry_run_do_not_write(settings, store):
    from core import capture_clients

    sp = RecentSpotify(page(item()))
    settings.quick_likes_enabled = False
    assert quick_likes.run(settings, spotify=sp, now=NOW)["skipped"] == "disabled"
    assert sp.reads == 0 and not store
    settings.quick_likes_enabled = True
    store[capture_clients.SPOTIFY_GATE_KEY] = gzip.compress(
        json.dumps({"retry_at": "2031-01-01T00:00:00Z"}).encode()
    )
    assert quick_likes.run(settings, spotify=sp, now=NOW)["skipped"] == "cooldown"
    assert sp.reads == 0
    store.clear()
    assert quick_likes.run(settings, spotify=sp, now=NOW, dry_run=True)["valid"] is True
    assert store == {} and sp.calls == []


def test_like_delivery_does_not_create_shazam_history(settings, store):
    from core import capture

    sp = RecentSpotify(page(item()))
    hub = FakeHub(INBOX)
    out = capture.capture(
        {"title": "Song", "artist": "Artist", "capture_id": "like-event"},
        sp,
        hub,
        settings,
        NOW,
        resolved_track=item()["track"],
        use_cache=False,
        liked_at=item()["added_at"],
    )
    assert out["ok"] and len(sp.calls) == 1
    songs = [r for table, rows in hub.pushed if table == "songs" for r in rows]
    assert any(
        r.get("liked") == 1 and r.get("liked_at") == "2030-01-01T00:00:00.000Z" for r in songs
    )
    assert all("shazam_count" not in r for r in songs)
    assert not any("/events/" in k for k in store)
    assert all(
        r.get("from_kind") != "shazam"
        for table, rows in hub.pushed
        if table == "provenance"
        for r in rows
    )


def test_curated_auto_like_observation_does_not_enter_inbox(settings, store):
    from core import capture

    saved = item()
    sp = RecentSpotify(page(saved))
    hub = FakeHub(
        [*INBOX, {"id": "curated", "kind": "curated"}],
        memberships=[
            {
                "id": "curated:song",
                "playlist_id": "curated",
                "isrc": "USAAA2600001",
                "added_at": "2029-01-01T00:00:00Z",
            }
        ],
    )
    out = capture.capture(
        {"title": "Song", "artist": "Artist", "capture_id": "like-event"},
        sp,
        hub,
        settings,
        NOW,
        resolved_track=saved["track"],
        use_cache=False,
        liked_at=saved["added_at"],
    )
    assert out["ok"] and out["skipped"] == "curated_like"
    assert sp.calls == []


def test_pending_quick_likes_blocks_full_sync_before_provider(settings, store):
    from core import archive, run

    store[archive.pending_key(settings)] = gzip.compress(
        json.dumps({"intent": "quick_likes"}).encode()
    )
    out = run.reconcile_run(settings, spotify=object(), hub=object())
    assert out.skipped and not out.applied


def test_new_like_checkpoint_restarts_without_readding(settings, store):
    from core import archive
    from core.hub import HubError

    settings.quick_likes_enabled = True
    sp = RecentSpotify(page(item("a")))
    hub = FakeHub(INBOX)
    assert quick_likes.run(settings, spotify=sp, hub=hub, now=NOW)["baseline"]
    sp.body = page(item("b", "2030-01-02T00:00:00Z"), item("a"))
    sp.tracks = [r["track"] for r in sp.body["items"]]
    original = hub.push

    def fail(table, rows):
        raise HubError("maintenance unavailable")

    hub.push = fail
    with pytest.raises(HubError):
        quick_likes.run(settings, spotify=sp, hub=hub, now=NOW)
    pending = json.loads(gzip.decompress(store[archive.pending_key(settings)]))
    assert pending["intent"] == "quick_likes" and pending["events"][0]["outcome"] == "added"
    hub.push = original
    reads = sp.reads
    assert quick_likes.run(settings, spotify=sp, hub=hub, now=NOW)["completed"] == 1
    assert sp.reads == reads and len([c for c in sp.calls if c[0] == "add"]) == 1
    assert json.loads(gzip.decompress(store[archive.pending_key(settings)])) is None


def test_unknown_attempt_without_positive_inbox_evidence_is_never_replayed(settings, store):
    from core import archive

    settings.quick_likes_enabled = True
    sp = RecentSpotify(page(item()))
    hub = FakeHub(INBOX)
    pending = {
        "intent": "quick_likes",
        "head": {},
        "source_ref": "raw/spotify-pull/quick-likes/test",
        "events": [{"item": item(), "outcome": "unknown", "done": False}],
    }
    store[archive.pending_key(settings)] = gzip.compress(json.dumps(pending).encode())
    with pytest.raises(ValueError, match="uncertain"):
        quick_likes.run(settings, spotify=sp, hub=hub, now=NOW)
    assert sp.calls == [] and sp.reads == 0


@pytest.mark.parametrize("hour", [1, 7, 13, 19])
def test_full_sync_slots_are_preserved(hour):
    assert quick_likes.cron_operation(NOW.replace(hour=hour, minute=40)) == "reconcile"
    assert quick_likes.cron_operation(NOW.replace(hour=hour, minute=25)) == "quick_likes"


def test_stale_tick_skips_before_any_state_or_provider_access(settings, monkeypatch):
    from datetime import timedelta
    from core import archive

    monkeypatch.setattr(archive, "get", lambda *a: pytest.fail("stale tick touched state"))
    assert (
        quick_likes.run(
            settings,
            spotify=object(),
            now=NOW,
            scheduled_at=(NOW - timedelta(minutes=16)).isoformat(),
        )["skipped"]
        == "stale_tick"
    )


def test_full_anchor_catches_up_a_nonoverlapping_window(settings, store):
    quick_likes.anchor(settings, [item("old")])
    quick_likes.anchor(settings, [item("new")])
    assert (
        quick_likes.diff(
            json.loads(gzip.decompress(store[quick_likes.key(settings)])), page(item("new"))
        )[1]
        == []
    )


@pytest.mark.parametrize(
    "body",
    [
        page({"track": None, "added_at": "2030-01-01T00:00:00Z"}),
        {"items": [item()], "offset": 0, "total": 99, "next": None},
        page(item("a", "not-a-time")),
    ],
)
def test_incomplete_positive_fact_cannot_be_applied(body):
    with pytest.raises(ValueError):
        quick_likes.diff(None, body)


def test_unchanged_window_has_no_state_or_catalog_writes(settings, store, monkeypatch):
    from core import archive

    settings.quick_likes_enabled = True
    sp = RecentSpotify(page(item()))
    quick_likes.run(settings, spotify=sp, now=NOW)
    monkeypatch.setattr(archive, "put", lambda *a: pytest.fail("unchanged window wrote state"))
    assert quick_likes.run(settings, spotify=sp, now=NOW)["changes"] == 0


def test_missing_isrc_or_foreign_track_identity_stops_before_state():
    body = page(item())
    body["items"][0]["track"]["uri"] = "spotify:track:other"
    with pytest.raises(ValueError):
        quick_likes.diff(None, body)
    body = page(item())
    body["items"][0]["track"]["external_ids"] = {}
    with pytest.raises(ValueError):
        quick_likes.diff(None, body)


def test_new_provider_cooldown_is_retained_and_not_retried(settings, store, monkeypatch):
    import httpx
    from core import capture_clients

    settings.quick_likes_enabled = True
    sp = RecentSpotify(page(item()))
    calls = []

    def limited(market):
        calls.append(market)
        request = httpx.Request("GET", "https://api.spotify.com/v1/me/tracks")
        response = httpx.Response(429, request=request, headers={"Retry-After": "120"})
        raise httpx.HTTPStatusError("limited", request=request, response=response)

    sp.get_recent_likes = limited
    held = []
    monkeypatch.setattr(
        capture_clients, "defer_spotify", lambda *a, **kw: held.append(a) or "2030-01-02T00:02:00Z"
    )
    out = quick_likes.run(settings, spotify=sp, now=NOW)
    assert out["retry_at"] == "2030-01-02T00:02:00Z" and len(calls) == 1 and len(held) == 1
    assert store == {}


def test_checkpoint_failure_prevents_first_mutation(settings, store, monkeypatch):
    from core import archive

    settings.quick_likes_enabled = True
    sp = RecentSpotify(page(item("a")))
    hub = FakeHub(INBOX)
    quick_likes.run(settings, spotify=sp, now=NOW)
    sp.body = page(item("b", "2030-01-02T00:00:00Z"), item("a"))
    sp.tracks = [r["track"] for r in sp.body["items"]]
    original = archive.put

    def blocked(s, k, v):
        if k == archive.pending_key(s):
            raise RuntimeError("checkpoint failed")
        original(s, k, v)

    monkeypatch.setattr(archive, "put", blocked)
    with pytest.raises(RuntimeError):
        quick_likes.run(settings, spotify=sp, hub=hub, now=NOW)
    assert sp.calls == [] and hub.pushed == []


def test_crash_after_done_event_cannot_replay_spotify(settings, store, monkeypatch):
    from core import archive

    settings.quick_likes_enabled = True
    sp = RecentSpotify(page(item("a")))
    hub = FakeHub(INBOX)
    quick_likes.run(settings, spotify=sp, now=NOW)
    sp.body = page(item("b", "2030-01-02T00:00:00Z"), item("a"))
    sp.tracks = [r["track"] for r in sp.body["items"]]
    original = archive.put

    def blocked(s, k, v):
        if k == quick_likes.key(s):
            raise RuntimeError("baseline checkpoint failed")
        original(s, k, v)

    monkeypatch.setattr(archive, "put", blocked)
    with pytest.raises(RuntimeError):
        quick_likes.run(settings, spotify=sp, hub=hub, now=NOW)
    monkeypatch.setattr(archive, "put", original)
    before = (sp.reads, len(sp.calls))
    quick_likes.run(settings, spotify=sp, hub=hub, now=NOW)
    assert (sp.reads, len(sp.calls)) == before


def test_song_patch_does_not_hide_unfinished_membership_and_provenance(settings, store):
    from core.hub import HubError

    settings.quick_likes_enabled = True
    sp = RecentSpotify(page(item("a")))
    hub = FakeHub(INBOX)
    quick_likes.run(settings, spotify=sp, hub=hub, now=NOW)
    sp.body = page(item("b", "2030-01-02T00:00:00Z"), item("a"))
    sp.tracks = [r["track"] for r in sp.body["items"]]
    original = hub.push
    failed = []

    def partial(table, rows):
        if table == "provenance" and not failed:
            failed.append(True)
            raise HubError("evidence failed after songs")
        for row in rows:
            existing = next((r for r in hub.tables[table] if r["id"] == row["id"]), None)
            if existing is None:
                hub.tables[table].append(dict(row))
            else:
                existing.update(row)
        return original(table, rows)

    hub.push = partial
    with pytest.raises(HubError):
        quick_likes.run(settings, spotify=sp, hub=hub, now=NOW)
    assert quick_likes.run(settings, spotify=sp, hub=hub, now=NOW)["completed"] == 1
    assert any(r.get("from_kind") == "spotify_like" for r in hub.tables["provenance"])
    assert len([c for c in sp.calls if c[0] == "add"]) == 1


def test_review_retry_without_pending_never_starts_new_work(settings, store):
    settings.quick_likes_enabled = True
    assert (
        quick_likes.run(settings, spotify=object(), now=NOW, retry_review=True)["skipped"]
        == "no_pending_recovery"
    )


def test_review_hold_does_not_poll_again_and_positive_evidence_can_finish(settings, store):
    from core import archive

    settings.quick_likes_enabled = True
    sp = RecentSpotify(page(item()))
    hub = FakeHub(INBOX)
    pending = {
        "intent": "quick_likes",
        "head": {},
        "source_ref": "raw/spotify-pull/quick-likes/test",
        "events": [{"item": item(), "outcome": "unknown", "done": False}],
    }
    store[archive.pending_key(settings)] = gzip.compress(json.dumps(pending).encode())
    with pytest.raises(ValueError):
        quick_likes.run(settings, spotify=sp, hub=hub, now=NOW)
    assert quick_likes.run(settings, spotify=sp, hub=hub, now=NOW)["skipped"] == "review_required"
    sp.inbox_items = [{"added_at": "2030-01-02T00:00:00Z", "item": item()["track"]}]
    assert (
        quick_likes.run(settings, spotify=sp, hub=hub, now=NOW, retry_review=True)["completed"] == 1
    )
    assert sp.calls == [] and sp.reads == 0


def test_small_scheduler_delay_does_not_turn_full_tick_into_light_poll():
    assert quick_likes.cron_operation(NOW.replace(hour=7, minute=41, second=5)) == "reconcile"
    assert quick_likes.cron_operation(NOW.replace(hour=7, minute=55)) == "quick_likes"


def test_read_only_429_check_cannot_extend_persisted_gate(settings, store, monkeypatch):
    import httpx
    from core import capture_clients

    sp = RecentSpotify(page(item()))
    saved = []

    def limited(market):
        req = httpx.Request("GET", "https://api.spotify.com/v1/me/tracks")
        resp = httpx.Response(429, request=req, headers={"Retry-After": "120"})
        raise httpx.HTTPStatusError("limited", request=req, response=resp)

    sp.get_recent_likes = limited
    monkeypatch.setattr(
        capture_clients, "defer_spotify", lambda *a, **kw: saved.append(kw.get("save")) or "later"
    )
    assert quick_likes.run(settings, spotify=sp, now=NOW, dry_run=True)["retry_at"] == "later"
    assert saved == [False] and store == {}
