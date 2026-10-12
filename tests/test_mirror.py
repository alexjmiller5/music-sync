import json
from pathlib import Path

import pytest

from core import mirror
from core.model import Playlist
from tests.hubsim import HubReads

FIX = Path(__file__).parent / "fixtures"
PLAYLISTS = json.loads((FIX / "live_playlists.json").read_text())
ITEMS = json.loads((FIX / "live_items.json").read_text())
LIKED = json.loads((FIX / "live_liked.json").read_text())

# Supported provenance columns used by Music Sync, independent of production projections.
PROVENANCE_COLUMNS = {
    "id",
    "from_kind",
    "from_ref",
    "to_kind",
    "to_ref",
    "rel",
    "asserted_by",
    "detail",
    "updated_at",
    "deleted_at",
}


class FakeHub:
    def __init__(self, tables):
        self.tables = tables

    def pull(self, table, columns, since="", where=None):
        if table == "provenance":
            assert not set(columns) - (PROVENANCE_COLUMNS | {"hub_at"}), (
                "unknown provenance projection"
            )
        rows = self.tables.get(table, [])
        rows = [r for r in rows if all(r.get(k) == v for k, v in (where or {}).items())]
        return [{c: r.get(c) for c in columns} for r in rows]

    def push(self, table, rows):
        for row in rows:
            if table == "provenance":
                assert not row.keys() - PROVENANCE_COLUMNS, "unknown provenance write"
            existing = next(
                (r for r in self.tables.setdefault(table, []) if r["id"] == row["id"]), None
            )
            if existing is None:
                self.tables[table].append(dict(row))
            else:
                existing.update(row)
        return {"upserted": len(rows), "rejected": []}


class FakeSpotify:
    def __init__(self):
        self.item_calls = []

    def get_playlists(self):
        return PLAYLISTS

    def get_playlist_items(self, pid, market):
        self.item_calls.append(pid)
        return ITEMS[pid]

    def get_liked(self, market):
        return LIKED


def test_load_mirror_splits_deleted_memberships_and_parses_json():
    hub = FakeHub(
        {
            "songs": [
                {
                    "id": "USUM71703861",
                    "liked": 1,
                    "liked_at": "t",
                    "first_seen": "t",
                    "spotify_ids": '["a","b"]',
                    "spotify_playable": 1,
                    "first_year": 2017,
                    "deezer_genres": '["Pop"]',
                    "mb_tags": "[]",
                    "title": "x",
                    "artists": '["y"]',
                    "deleted_at": None,
                }
            ],
            "playlists": [
                {
                    "id": "P",
                    "name": "genre picks",
                    "kind": "smart",
                    "rule": '{"v":1}',
                    "description": None,
                    "snapshot_id": "s1",
                    "pinned": 1,
                    "expires_at": None,
                    "deleted_at": None,
                }
            ],
            "playlist_songs": [
                {
                    "id": "P:USUM71703861",
                    "playlist_id": "P",
                    "isrc": "USUM71703861",
                    "spotify_track_id": "a",
                    "added_at": "t",
                    "deleted_at": None,
                },
                {
                    "id": "P:GBBTV1101287",
                    "playlist_id": "P",
                    "isrc": "GBBTV1101287",
                    "spotify_track_id": "c",
                    "added_at": "t",
                    "deleted_at": "t2",
                },
            ],
            "provenance": [
                {
                    "id": "shazam:1:USUM71703861",
                    "from_kind": "shazam",
                    "to_kind": "songs",
                    "to_ref": "USUM71703861",
                    "rel": "imported_from",
                    "deleted_at": None,
                },
                {
                    "id": "x",
                    "from_kind": "photo",
                    "to_kind": "places",
                    "to_ref": "z",
                    "rel": "evidence_of",
                    "deleted_at": None,
                },
            ],
        }
    )
    m = mirror.load_mirror(hub)
    assert m.songs["USUM71703861"].spotify_ids == ["a", "b"]
    assert m.playlists["P"].rule == {"v": 1}
    assert list(m.memberships) == [("P", "USUM71703861")]
    assert m.deleted_memberships[0].isrc == "GBBTV1101287"
    assert m.captures == {("USUM71703861", "shazam")}


def test_item_from_raw_validates_isrc_and_local():
    raw = ITEMS["P1"][0]
    it = mirror.item_from_raw(raw)
    assert it.isrc == raw["item"]["external_ids"]["isrc"].upper()
    assert it.track_id == raw["item"]["id"] and it.added_at == raw["added_at"]
    local = {
        "added_at": "t",
        "item": {
            "id": None,
            "uri": "spotify:local:x",
            "is_local": True,
            "name": "n",
            "artists": [],
            "external_ids": {},
        },
    }
    assert mirror.item_from_raw(local).isrc is None and mirror.item_from_raw(local).is_local


@pytest.mark.parametrize("envelope", ["item", "track"])
@pytest.mark.parametrize("playable", [None, False, True])
def test_normalize_metadata_envelopes_without_inventing_availability(envelope, playable):
    track = {
        "id": "observed",
        "external_ids": {"isrc": " usaaa2600001 "},
        "name": "Observed",
        "artists": [{"name": "Artist"}],
        "album": {"name": "Release", "release_date": "2018-02-03"},
        "duration_ms": 123456,
        "linked_from": {"id": "original"},
    }
    if playable is not None:
        track["is_playable"] = playable
    it = mirror.item_from_raw({envelope: track})
    assert it.playable is playable
    assert (it.album, it.album_year, it.duration_ms, it.linked_from_id) == (
        "Release",
        2018,
        123456,
        "original",
    )
    assert it.isrc == "USAAA2600001"


def test_load_mirror_retains_base_fields_and_only_direct_observation_provenance():
    observation = {
        "id": "observation",
        "from_kind": "takeout",
        "from_ref": "raw/spotify-pull/x.json.gz",
        "to_kind": "songs",
        "to_ref": "USAAA2600001",
        "rel": "evidence_of",
        "asserted_by": "music-sync",
        "detail": '{"kind":"spotify_observation","field":"title","track_id":"observed","value":"Title","observed_at":"2026-09-12T12:00:00.000Z"}',
    }
    m = mirror.load_mirror(
        FakeHub(
            {
                "songs": [
                    {
                        "id": "USAAA2600001",
                        "album": "Release",
                        "album_year": 2018,
                        "duration_ms": 123456,
                    }
                ],
                "provenance": [
                    observation,
                    {**observation, "id": "deleted", "deleted_at": "t"},
                    {**observation, "id": "legacy", "rel": "derived_from"},
                ],
            }
        )
    )
    s = m.songs["USAAA2600001"]
    assert (s.album, s.album_year, s.duration_ms) == ("Release", 2018, 123456)
    assert [r["id"] for r in m.observations] == ["observation"]
    assert m.observations[0]["detail"]["track_id"] == "observed"
    assert m.observations[0]["detail"]["observed_at"] == "2026-09-12T12:00:00.000Z"


def test_pull_live_skips_unchanged_snapshots_and_foreign_playlists():
    m = mirror.Mirror(
        songs={},
        playlists={
            "P1": Playlist("P1", "a", "curated", None, None, PLAYLISTS[0]["snapshot_id"], 1, None)
        },
        memberships={},
        deleted_memberships=[],
        captures=set(),
    )
    sp = FakeSpotify()
    live = mirror.pull_live(sp, "US", "alexmiller", m)
    assert set(live.playlists) == {p["id"] for p in PLAYLISTS if p["owner"]["id"] == "alexmiller"}
    assert live.playlists["P1"].items is None and sp.item_calls == ["P2"]
    assert all(k for k in live.liked) and live.raw["liked"] == LIKED
    assert [p["id"] for p in live.raw["playlists"]] == [p["id"] for p in PLAYLISTS]


def test_pull_live_always_fetches_smart_playlists_even_if_snapshot_unchanged():
    m = mirror.Mirror(
        songs={},
        playlists={
            "P1": Playlist("P1", "a", "smart", {"v": 1}, None, PLAYLISTS[0]["snapshot_id"], 1, None)
        },
        memberships={},
        deleted_memberships=[],
        captures=set(),
    )
    sp = FakeSpotify()
    live = mirror.pull_live(sp, "US", "alexmiller", m)
    assert live.playlists["P1"].items is not None
    assert sp.item_calls == ["P1", "P2"]


class SimHub(FakeHub, HubReads):
    """FakeHub tables behind the hub's paged read protocol (tests/hubsim.py)."""

    def __init__(self, tables, page=200):
        super().__init__(tables)
        self.page, self.reads, self.pulls = page, [], []

    def pull(self, table, columns, since="", where=None):
        return HubReads.pull(self, table, columns, since, where)

    def read(self, route, body):
        self.reads.append(route)
        return HubReads.read(self, route, body)

    def read_page(self, table, columns, since="", limit=200, after=None, where=None):
        if after is None:
            self.pulls.append((table, since))
        return HubReads.read_page(self, table, columns, since, limit, after, where)


def stamped_tables():
    song = {
        "id": "USAAA2600001",
        "liked": 1,
        "liked_at": "2026-01-01T00:00:00.000Z",
        "first_seen": "2026-01-01T00:00:00.000Z",
        "title": "One",
        "hub_at": "2026-10-01T00:00:00.000Z",
    }
    playlist = {
        "id": "P",
        "name": "list",
        "kind": "curated",
        "pinned": 1,
        "hub_at": "2026-10-01T00:00:00.000Z",
    }
    return {"songs": [song], "playlists": [playlist], "playlist_songs": [], "provenance": []}


def test_cached_mirror_pulls_only_changes_and_applies_soft_deletes():
    tables = stamped_tables()
    hub, cache = SimHub(tables), mirror.new_cache(now=1000)
    first = mirror.load_mirror(hub, cache=cache, now=1000)
    assert set(first.songs) == {"USAAA2600001"} and all(s == "" for _, s in hub.pulls)
    tables["songs"][0].update(title="Renamed", hub_at="2026-10-02T00:00:00.000Z")
    tables["songs"].append(
        {**tables["songs"][0], "id": "USAAA2600002", "hub_at": "2026-10-02T00:00:00.000Z"}
    )
    tables["playlists"][0].update(
        deleted_at="2026-10-02T00:00:00.000Z", hub_at="2026-10-02T00:00:00.000Z"
    )
    hub.pulls.clear()
    second = mirror.load_mirror(hub, cache=json.loads(json.dumps(cache)), now=2000)
    assert ("songs", "2026-10-01T00:00:00.000Z") in hub.pulls
    assert second.songs["USAAA2600001"].title == "Renamed" and "USAAA2600002" in second.songs
    assert second.playlists == {}


def test_stale_cache_falls_back_to_a_full_pull():
    hub = SimHub(stamped_tables())
    cache = mirror.new_cache(now=0)
    mirror.load_mirror(hub, cache=cache, now=0)
    hub.pulls.clear()
    mirror.load_mirror(hub, cache=cache, now=mirror.FULL_REFRESH_SECONDS + 1)
    assert all(since == "" for _, since in hub.pulls)


def test_cache_is_checkpointed_after_each_step():
    hub, cache, saved = SimHub(stamped_tables()), mirror.new_cache(now=0), []
    mirror.load_mirror(
        hub,
        cache=cache,
        now=0,
        checkpoint=lambda: saved.append((bool(cache["pull"]), len(cache["slices"]))),
    )
    # The three tables in one helper round, then each provenance slice.
    assert saved == [(True, 0), (True, 1), (True, 2)]


def test_pull_live_reuses_a_smart_playlist_unchanged_since_its_last_write():
    from core import metadata
    from core.model import Membership, Mirror

    snap = PLAYLISTS[0]["snapshot_id"]
    member = Membership("P1", "USZZZ2600099", "t1", "2026-01-01T00:00:00.000Z")
    m = Mirror(
        songs={},
        playlists={"P1": Playlist("P1", "a", "smart", {"v": 1}, None, snap, 1, None)},
        memberships={("P1", "USZZZ2600099"): member},
        deleted_memberships=[],
        captures=set(),
    )
    sp = FakeSpotify()
    live = mirror.pull_live(sp, "US", "alexmiller", m, full=True, reuse_smart=True)
    assert "P1" not in sp.item_calls and "P2" in sp.item_calls  # others are still read
    items = live.playlists["P1"].items
    assert [(i.isrc, i.track_id, i.uri, i.added_at, i.observed) for i in items] == [
        ("USZZZ2600099", "t1", "spotify:track:t1", "2026-01-01T00:00:00.000Z", False)
    ]
    assert live.raw["reused"] == {"P1": snap} and "P1" not in live.raw["items"]
    # Rebuilt from the catalog, never a fresh Spotify observation of its metadata.
    assert "USZZZ2600099" not in metadata.observations(live)
    # A retained pull with reused playlists reads back the same way.
    again = mirror.live_from_raw(live.raw, "alexmiller", m)
    assert [i.uri for i in again.playlists["P1"].items] == ["spotify:track:t1"]
    # Only smart playlists are reused; a curated one with the same snapshot is read.
    m.playlists["P1"].kind, m.playlists["P1"].rule = "curated", None
    sp = FakeSpotify()
    live = mirror.pull_live(sp, "US", "alexmiller", m, full=True, reuse_smart=True)
    assert "P1" in sp.item_calls and "reused" not in live.raw


def test_pull_live_rereads_a_smart_playlist_changed_outside_music_sync():
    from core.model import Membership, Mirror

    m = Mirror(
        songs={},
        playlists={"P1": Playlist("P1", "a", "smart", {"v": 1}, None, "older", 1, None)},
        memberships={("P1", "X"): Membership("P1", "X", "t9", "2026-01-01T00:00:00.000Z")},
        deleted_memberships=[],
        captures=set(),
    )
    sp = FakeSpotify()
    live = mirror.pull_live(sp, "US", "alexmiller", m, full=True, reuse_smart=True)
    assert "P1" in sp.item_calls and "reused" not in live.raw
    assert all(it.observed for it in live.playlists["P1"].items)
    assert "spotify:track:t9" not in [it.uri for it in live.playlists["P1"].items]


def stamped_song(isrc, hub_at, **extra):
    return {"id": isrc, "liked": 0, "first_seen": hub_at, "hub_at": hub_at, **extra}


def test_cached_mirror_never_skips_a_row_committed_behind_the_walk():
    # Two writes land mid-walk: an earlier-stamped row behind the keyset position
    # and a later-stamped one ahead of it. A cursor taken from the rows received
    # would jump past the first one forever; the cursor read before the pull keeps it.
    t1, t2, t3, t4 = (f"2026-10-0{d}T00:00:00.000Z" for d in (1, 2, 3, 4))
    tables = stamped_tables()
    tables["songs"] = [stamped_song("USAAA2600001", t1), stamped_song("USAAA2600005", t1)]
    hub, cache = SimHub(tables, page=1), mirror.new_cache(now=0)
    mirror.load_mirror(hub, cache=cache, now=0)
    tables["songs"].append(stamped_song("USAAA2600009", t2))
    committed = []

    def commit_mid_walk():
        if not committed:
            committed.append(1)
            tables["songs"].append(stamped_song("USAAA2600000", t3))  # behind the walk
            tables["songs"].append(stamped_song("USAAA2600099", t4))  # ahead of it

    hub.on_page = commit_mid_walk
    mirror.load_mirror(hub, cache=cache, now=1)
    hub.on_page = None
    third = mirror.load_mirror(hub, cache=cache, now=2)
    assert {"USAAA2600000", "USAAA2600099"} <= set(third.songs)


def test_quiet_cached_mirror_round_is_one_cursor_request():
    hub, cache = SimHub(stamped_tables()), mirror.new_cache(now=0)
    mirror.load_mirror(hub, cache=cache, now=0)
    hub.reads.clear()
    pulls = []
    hub.scan = lambda *a, **kw: pulls.append(a) or []
    again = mirror.load_mirror(hub, cache=cache, now=1)
    assert hub.reads == ["/v1/cursor"] and pulls == []
    assert set(again.songs) == {"USAAA2600001"} and set(again.playlists) == {"P"}


def test_cached_mirror_rereads_provenance_slices_only_from_the_held_mark():
    t1, t2 = "2026-10-01T00:00:00.000Z", "2026-10-02T00:00:00.000Z"
    tables = stamped_tables()
    edge = {
        "id": "shazam:1:USAAA2600001",
        "from_kind": "shazam",
        "from_ref": "1",
        "to_kind": "songs",
        "to_ref": "USAAA2600001",
        "rel": "imported_from",
        "hub_at": t1,
    }
    tables["provenance"] = [edge]
    hub, cache = SimHub(tables), mirror.new_cache(now=0)
    first = mirror.load_mirror(hub, cache=cache, now=0)
    assert first.captures == {("USAAA2600001", "shazam")}
    # Another source's edge moves the table: both slices re-read from the held mark.
    tables["provenance"].append({**edge, "id": "x", "to_kind": "movies", "hub_at": t2})
    tables["provenance"].append(
        {**edge, "id": "takeout:2:USAAA2600001", "from_kind": "takeout", "hub_at": t2}
    )
    sinces = []
    scan = hub.scan
    hub.scan = lambda table, columns, *, where=None, since="": (
        sinces.append((table, since)) or scan(table, columns, where=where, since=since)
    )
    second = mirror.load_mirror(hub, cache=cache, now=1)
    assert sinces == [("provenance", t1), ("provenance", t1)]
    assert second.captures == {("USAAA2600001", "shazam"), ("USAAA2600001", "takeout")}
    # A quiet table is not re-read at all.
    sinces.clear()
    mirror.load_mirror(hub, cache=cache, now=2)
    assert sinces == []


def test_cached_mirror_drops_purged_rows_when_a_mark_moves_back():
    # A purge of a table's newest rows moves its mark behind the held cursor:
    # the table, or the provenance slice, is read whole again and the purged row is gone.
    t1, t2 = "2026-10-01T00:00:00.000Z", "2026-10-02T00:00:00.000Z"
    tables = stamped_tables()
    tables["songs"].append(stamped_song("USAAA2600002", t2))
    edge = {
        "id": "shazam:2:USAAA2600002",
        "from_kind": "shazam",
        "from_ref": "2",
        "to_kind": "songs",
        "to_ref": "USAAA2600002",
        "rel": "imported_from",
        "hub_at": t2,
    }
    older = {**edge, "id": "shazam:1:USAAA2600001", "to_ref": "USAAA2600001", "hub_at": t1}
    tables["provenance"] = [older, edge]
    hub, cache = SimHub(tables), mirror.new_cache(now=0)
    first = mirror.load_mirror(hub, cache=cache, now=0)
    assert "USAAA2600002" in first.songs and ("USAAA2600002", "shazam") in first.captures
    tables["songs"].pop()
    tables["provenance"].pop()
    second = mirror.load_mirror(hub, cache=cache, now=1)
    assert set(second.songs) == {"USAAA2600001"}
    assert second.captures == {("USAAA2600001", "shazam")}


def test_provenance_slice_never_skips_an_edge_committed_behind_the_walk():
    t1, t2, t3, t4 = (f"2026-10-0{d}T00:00:00.000Z" for d in (1, 2, 3, 4))

    def edge(n, hub_at):
        isrc = f"USAAA260000{n}"
        return {
            "id": f"shazam:{n}:{isrc}",
            "from_kind": "shazam",
            "from_ref": str(n),
            "to_kind": "songs",
            "to_ref": isrc,
            "rel": "imported_from",
            "hub_at": hub_at,
        }

    tables = stamped_tables()
    tables["provenance"] = [edge(1, t1)]
    hub, cache = SimHub(tables, page=1), mirror.new_cache(now=0)
    mirror.load_mirror(hub, cache=cache, now=0)
    tables["provenance"].append(edge(5, t2))
    committed = []

    def commit_mid_walk():
        if not committed:
            committed.append(1)
            tables["provenance"] += [edge(0, t3), edge(9, t4)]  # behind and ahead

    hub.on_page = commit_mid_walk
    mirror.load_mirror(hub, cache=cache, now=1)
    hub.on_page = None
    third = mirror.load_mirror(hub, cache=cache, now=2)
    assert {("USAAA2600000", "shazam"), ("USAAA2600009", "shazam")} <= third.captures
