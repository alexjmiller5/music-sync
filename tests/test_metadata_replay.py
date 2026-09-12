"""Retained observations replay through real planning/apply with in-memory service boundaries."""

import gzip
import json
from copy import deepcopy

import pytest

from core import archive, flags, spotify_client
from tests.test_metadata import raw
from tests.test_release_regressions import A, B, Store

KEY = "raw/spotify-pull/example.json.gz"
STAMP = "2026-01-01T00:00:00.000Z"


@pytest.fixture
def replay_env(monkeypatch):
    hub = Store(liked=0)
    hub.songs = hub.tables["songs"]
    hub.pushes = []
    hub.songs[A].update(spotify_ids=["observed"], spotify_playable=None)
    hub.tables["provenance"]["old-derivation"] = {
        "id": "old-derivation",
        "from_kind": "derivation",
        "to_kind": "songs",
        "to_ref": A,
        "rel": "derived_from",
        "detail": {"source": "old"},
    }
    hub.objects = {
        KEY: gzip.compress(
            json.dumps(
                {
                    "playlists": [],
                    "items": {},
                    "liked": [raw()],
                }
            ).encode()
        )
    }
    hub.saves = []
    original_push = hub.push

    def push(table, rows):
        # Every hub mutation has durable, stamped, metadata-only intent first.
        pending = json.loads(gzip.decompress(hub.objects[archive.PENDING_KEY]))
        assert pending["intent"] == "metadata_replay" and pending["writes"] is False
        assert all(op["kind"] == "hub" for op in pending["operations"])
        assert all("updated_at" in r for op in pending["operations"] for r in op["rows"])
        original_push(table, rows)
        hub.pushes.append((table, deepcopy(rows)))

    def put(settings, key, data):
        assert key == archive.PENDING_KEY
        hub.saves.append(bytes(data))
        hub.objects[key] = bytes(data)

    def forbidden(*args, **kwargs):
        pytest.fail("unexpected Spotify/provider/Notion call")

    hub.push = push
    monkeypatch.setattr(archive, "get", lambda s, k: hub.objects.get(k))
    monkeypatch.setattr(archive, "put", put)
    monkeypatch.setattr(spotify_client, "SpotifyClient", forbidden)
    monkeypatch.setattr(flags, "file", forbidden)
    monkeypatch.setattr(hub, "derive", forbidden, raising=False)
    return KEY, STAMP, hub


def test_replay_only_fills_metadata_and_is_idempotent(settings, replay_env):
    from core import metadata_replay

    key, stamp, hub = replay_env
    before = deepcopy(hub.tables)
    out = metadata_replay.run(settings, key, stamp, dry_run=False, hub=hub)
    assert out["recovered"] == 1 and not out["errors"]
    assert hub.songs[A]["title"] == "Observed title"
    assert hub.songs[A]["liked"] == before["songs"][A]["liked"] == 0
    assert hub.songs[A]["liked_at"] is None
    assert hub.songs[A]["first_seen"] == before["songs"][A]["first_seen"]
    assert hub.tables["playlists"] == before["playlists"]
    assert hub.tables["playlist_songs"] == before["playlist_songs"]
    assert hub.tables["provenance"]["old-derivation"] == before["provenance"]["old-derivation"]
    edge = hub.tables["provenance"][f"spotify-observation:{A}:title"]
    assert edge["from_ref"] == key and edge["detail"]["observed_at"] == stamp
    calls, saved, after = len(hub.pushes), len(hub.saves), deepcopy(hub.tables)
    again = metadata_replay.run(settings, key, stamp, dry_run=False, hub=hub)
    assert again["already_present"] == 1 and again["recovered"] == 0
    assert len(hub.pushes) == calls and len(hub.saves) == saved and hub.tables == after


def test_default_dry_run_has_no_writes_or_timestamps(settings, replay_env):
    from core import metadata_replay

    key, stamp, hub = replay_env
    before = deepcopy((hub.tables, hub.objects))
    out = metadata_replay.run(settings, key, stamp, hub=hub)
    assert out["dry_run"] is True and not out["applied"]
    assert out["planned"] and all("updated_at" not in a["row"] for a in out["planned"])
    assert not hub.pushes and not hub.saves
    assert (hub.tables, hub.objects) == before


@pytest.mark.parametrize("envelope", ["pull-items", "capture-old", "capture-new"])
def test_accepts_normalized_pull_and_capture_archives(settings, replay_env, envelope):
    from core import metadata_replay

    key, _, hub = replay_env
    if envelope == "pull-items":
        body = {"playlists": [{"id": "P"}], "items": {"P": [raw()]}, "liked": []}
    else:
        key = "raw/spotify-capture/example.json.gz"
        body = {"playlist_id": "P", "items": [raw()] if envelope == "capture-old" else []}
        if envelope == "capture-new":
            body["resolved_track"] = raw()["track"]
    hub.objects[key] = gzip.compress(json.dumps(body).encode())
    out = metadata_replay.run(
        settings, key, "2026-01-01T01:00:00.000999+01:00", dry_run=False, hub=hub
    )
    assert out["recovered"] == 1
    assert (
        hub.tables["provenance"][f"spotify-observation:{A}:title"]["detail"]["observed_at"] == STAMP
    )


@pytest.mark.parametrize(
    "key",
    [
        None,
        1,
        [],
        "",
        "raw/spotify-pull/",
        "/raw/spotify-pull/a.json.gz",
        "raw/other/a.json.gz",
        "raw/spotify-pull/../a.json.gz",
        "raw/spotify-pull/./a.json.gz",
        "raw/spotify-pull//a.json.gz",
        "raw/spotify-pull/%2e%2e/a.json.gz",
        "raw/spotify-pull/a\\b.json.gz",
        "raw/spotify-pull/a.json.gz?x",
        "raw/spotify-pull/a.json",
    ],
)
def test_rejects_bad_keys_before_io(settings, replay_env, monkeypatch, key):
    from core import metadata_replay

    def forbidden(*args):
        pytest.fail("invalid request reached archive")

    monkeypatch.setattr(archive, "get", forbidden)
    with pytest.raises(ValueError):
        metadata_replay.run(settings, key, STAMP, dry_run=False, hub=replay_env[2])


@pytest.mark.parametrize(
    "stamp", [None, 0, [], "", "2026-01-01", "2026-01-01T00:00:00", "2026-02-30T00:00:00Z"]
)
def test_rejects_invalid_or_naive_time(settings, replay_env, stamp):
    from core import metadata_replay

    with pytest.raises(ValueError):
        metadata_replay.run(settings, KEY, stamp, dry_run=False, hub=replay_env[2])
    assert not replay_env[2].pushes and not replay_env[2].saves


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        {},
        {"liked": []},
        {"playlists": [], "items": [], "liked": []},
        {"playlists": [], "items": {}, "liked": {}},
        {"playlists": [], "items": {}, "liked": [1]},
        {"playlists": [], "items": {}, "liked": [{}]},
        {"playlists": [], "items": {}, "liked": [raw(name=4)]},
        {"playlists": [], "items": {}, "liked": [raw(duration_ms="123")]},
        {"playlists": [], "items": {}, "liked": [raw(duration_ms=True)]},
        {"playlists": [], "items": {}, "liked": [raw(is_playable="false")]},
        {"playlists": [], "items": {}, "liked": [raw(artists=[{"name": []}])]},
        {"playlists": [], "items": {}, "liked": [raw(album={"release_date": 2018})]},
    ],
)
def test_rejects_malformed_envelopes_without_writes(settings, replay_env, body):
    from core import metadata_replay

    key, stamp, hub = replay_env
    hub.objects[key] = gzip.compress(json.dumps(body).encode())
    with pytest.raises(ValueError):
        metadata_replay.run(settings, key, stamp, dry_run=False, hub=hub)
    assert not hub.pushes and not hub.saves


def test_missing_and_corrupt_archives_do_not_write(settings, replay_env):
    from core import metadata_replay

    key, stamp, hub = replay_env
    del hub.objects[key]
    with pytest.raises(FileNotFoundError):
        metadata_replay.run(settings, key, stamp, dry_run=False, hub=hub)
    hub.objects[key] = b"broken gzip"
    with pytest.raises(ValueError):
        metadata_replay.run(settings, key, stamp, dry_run=False, hub=hub)
    assert not hub.pushes and not hub.saves


def test_conflict_takes_precedence_over_fills_and_missing_rows_are_not_created(
    settings, replay_env
):
    from core import metadata_replay

    key, stamp, hub = replay_env
    hub.songs[A].update(title="Current title", album="Current album", first_year=1970)
    hub.objects[key] = gzip.compress(
        json.dumps(
            {
                "playlists": [],
                "items": {},
                "liked": [raw(), raw(external_ids={"isrc": B})],
            }
        ).encode()
    )
    out = metadata_replay.run(settings, key, stamp, dry_run=False, hub=hub)
    assert (out["conflicting"], out["recovered"], out["missing_source"], out["failed"]) == (
        1,
        0,
        1,
        0,
    )
    assert out["applied"]["upsert_song"] == 1
    assert hub.songs[A]["title"] == "Current title" and hub.songs[A]["album"] == "Current album"
    assert not hub.songs[A].get("album_year") and hub.songs[A]["first_year"] == 1970
    assert hub.songs[A]["artists"] == ["Observed artist"] and B not in hub.songs
    assert {c["field"] for r in out["rows"] for c in r.get("conflicts", [])} >= {"title", "album"}
    assert f"spotify-observation:{A}:title" not in hub.tables["provenance"]


@pytest.mark.parametrize("dry_run", [False, True])
def test_reconcile_intent_blocks_replay(settings, replay_env, dry_run):
    from core import metadata_replay

    key, stamp, hub = replay_env
    hub.objects[archive.PENDING_KEY] = gzip.compress(
        json.dumps(
            {
                "planned": [],
                "operations": [],
                "writes": False,
            }
        ).encode()
    )
    with pytest.raises(RuntimeError, match="[Pp]ending"):
        metadata_replay.run(settings, key, stamp, dry_run=dry_run, hub=hub)
    assert not hub.pushes and not hub.saves


def test_partial_failure_retries_original_provenance_without_replanning(settings, replay_env):
    from core import metadata_replay

    key, stamp, hub = replay_env
    hub.fail = "provenance"
    out = metadata_replay.run(settings, key, stamp, dry_run=False, hub=hub)
    assert out["failed"] == 1 and out["recovered"] == 0 and out["errors"]
    assert out["applied"]["upsert_song"] == 1 and hub.songs[A]["title"] == "Observed title"
    pending_bytes = hub.objects[archive.PENDING_KEY]
    pending = json.loads(gzip.decompress(pending_bytes))
    rows = pending["operations"][0]["rows"]
    assert pending["operations"][0]["table"] == "provenance"
    del hub.objects[key]  # Recovery needs saved intent, even if retention removed the source.
    before = deepcopy((hub.tables, hub.objects, hub.pushes, hub.saves))
    preview = metadata_replay.run(settings, key, stamp, hub=hub)
    assert preview["dry_run"] and not preview["applied"]
    assert (hub.tables, hub.objects, hub.pushes, hub.saves) == before
    with pytest.raises(RuntimeError, match="[Pp]ending"):
        metadata_replay.run(settings, key, "2026-01-02T00:00:00Z", dry_run=False, hub=hub)
    out = metadata_replay.run(settings, key, stamp, dry_run=False, hub=hub)
    assert out["recovered"] == 1 and not out["errors"]
    assert all(hub.tables["provenance"][r["id"]] == r for r in rows)
    assert len([1 for table, _ in hub.pushes if table == "songs"]) == 1
    assert json.loads(gzip.decompress(hub.objects[archive.PENDING_KEY])) is None


@pytest.mark.parametrize("source_market", [None, "GB"])
def test_availability_uses_only_retained_market_and_retry_keeps_it(
    settings, replay_env, source_market
):
    from core import metadata_replay

    key, stamp, hub = replay_env
    body = json.loads(gzip.decompress(hub.objects[key]))
    if source_market is not None:
        body["market"] = source_market
    # A relinking object may retain more than its ID in a full track response.
    body["liked"][0]["track"]["linked_from"] = {
        "id": "original",
        "external_urls": {"spotify": "https://example.test/track"},
    }
    hub.objects[key] = gzip.compress(json.dumps(body).encode())
    hub.fail = "provenance"
    out = metadata_replay.run(settings, key, stamp, dry_run=False, hub=hub)
    assert out["failed"] == 1
    pending = json.loads(gzip.decompress(hub.objects[archive.PENDING_KEY]))
    evidence = [a["row"] for a in pending["planned"] if a["kind"] == "edge"]
    assert evidence and all(r["detail"]["market"] == source_market for r in evidence)
    settings.spotify_market = "CA"
    assert not metadata_replay.run(settings, key, stamp, dry_run=False, hub=hub)["errors"]
    assert all(
        hub.tables["provenance"][r["id"]]["detail"]["market"] == source_market for r in evidence
    )


def test_missing_metadata_without_recoverable_source_is_a_gap(settings, replay_env):
    from core import metadata_replay

    key, stamp, hub = replay_env
    hub.objects[key] = gzip.compress(
        json.dumps(
            {
                "playlists": [],
                "items": {},
                "liked": [
                    raw(
                        name=None,
                        artists=[],
                        album=None,
                        duration_ms=None,
                        is_playable=None,
                    )
                ],
            }
        ).encode()
    )
    out = metadata_replay.run(settings, key, stamp, dry_run=False, hub=hub)
    assert out["missing_source"] == 1 and out["already_present"] == 0
    assert not hub.pushes and not hub.saves


def test_unchanged_facts_do_not_get_observation_labels(settings, replay_env):
    from core import metadata_replay

    key, stamp, hub = replay_env
    hub.songs[A].update(
        title="Observed title",
        artists=["Observed artist"],
        album="Observed release",
        album_year=2018,
        duration_ms=123456,
        spotify_playable=1,
    )
    out = metadata_replay.run(settings, key, stamp, dry_run=False, hub=hub)
    assert out["already_present"] == 1 and not out["planned"]
    assert list(hub.tables["provenance"]) == ["old-derivation"]
    assert not hub.pushes and not hub.saves


def test_existing_availability_conflict_is_reported_without_overwriting(settings, replay_env):
    from core import metadata_replay

    key, stamp, hub = replay_env
    hub.songs[A]["spotify_playable"] = 0
    out = metadata_replay.run(settings, key, stamp, dry_run=False, hub=hub)
    assert out["conflicting"] == 1 and out["recovered"] == 0
    assert hub.songs[A]["spotify_playable"] == 0
    assert {c["field"] for c in out["rows"][0]["conflicts"]} == {"spotify_playable"}
    assert all(
        r.get("detail", {}).get("field") != "spotify_playable"
        for r in hub.tables["provenance"].values()
    )


@pytest.mark.parametrize(
    "capture_body",
    [
        {"items": []},
        {"playlist_id": 1, "items": []},
        {"playlist_id": "P", "items": {}},
        {"playlist_id": "P", "items": [], "resolved_track": []},
        {"playlist_id": "P", "items": [], "market": 1},
        {"playlist_id": "P", "items": [], "market": "not a market"},
    ],
)
def test_rejects_malformed_capture_envelopes(settings, replay_env, capture_body):
    from core import metadata_replay

    _, stamp, hub = replay_env
    key = "raw/spotify-capture/example.json.gz"
    hub.objects[key] = gzip.compress(json.dumps(capture_body).encode())
    with pytest.raises(ValueError):
        metadata_replay.run(settings, key, stamp, dry_run=False, hub=hub)
    assert not hub.pushes and not hub.saves


def test_checkpoint_failure_stops_before_hub_mutations(settings, replay_env, monkeypatch):
    from core import metadata_replay

    key, stamp, hub = replay_env

    def fail(*args):
        raise RuntimeError("checkpoint unavailable")

    monkeypatch.setattr(archive, "put", fail)
    before = deepcopy(hub.tables)
    out = metadata_replay.run(settings, key, stamp, dry_run=False, hub=hub)
    assert out["failed"] == 1 and out["recovered"] == 0
    assert "checkpoint" in out["errors"][0]["message"]
    assert not hub.pushes and hub.tables == before
