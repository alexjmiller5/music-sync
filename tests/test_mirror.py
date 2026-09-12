import json
from pathlib import Path

import pytest

from core import mirror
from core.model import Playlist

FIX = Path(__file__).parent / "fixtures"
PLAYLISTS = json.loads((FIX / "live_playlists.json").read_text())
ITEMS = json.loads((FIX / "live_items.json").read_text())
LIKED = json.loads((FIX / "live_liked.json").read_text())


class FakeHub:
    def __init__(self, tables):
        self.tables = tables

    def pull(self, table, columns, since=""):
        return [{c: r.get(c) for c in columns} for r in self.tables.get(table, [])]


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
                    "name": "rap",
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
        "observed_at": "2026-09-12T12:00:00.000Z",
        "detail": '{"kind":"spotify_observation","field":"title","track_id":"observed","value":"Title"}',
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
