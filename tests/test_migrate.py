import importlib.util
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "migrate", Path(__file__).parents[1] / "scripts" / "migrate.py"
)
migrate = importlib.util.module_from_spec(spec)
sys.modules["migrate"] = migrate
spec.loader.exec_module(migrate)


def test_parse_args_step_and_dry_run():
    args = migrate.parse_args(["--step", "inbox", "--playlist-id", "P1", "--dry-run"])
    assert args.step == "inbox" and args.playlist_id == "P1" and args.dry_run


def test_parse_args_rejects_unknown_step():
    import pytest

    with pytest.raises(SystemExit):
        migrate.parse_args(["--step", "nope"])


class FakeHub:
    def __init__(self, tables=None):
        self.tables = tables or {}
        self.pushed = []

    def pull(self, table, columns, since=""):
        return [{c: r.get(c) for c in columns} for r in self.tables.get(table, [])]

    def push(self, table, rows):
        self.pushed.append((table, rows))
        return {"upserted": len(rows), "rejected": []}


def test_step_inbox_dry_run_pushes_nothing():
    hub = FakeHub()
    migrate.step_inbox(hub, "P1", dry_run=True)
    assert hub.pushed == []


def test_step_inbox_pushes_the_inbox_playlist_row():
    hub = FakeHub()
    migrate.step_inbox(hub, "P1", dry_run=False)
    table, rows = hub.pushed[0]
    assert table == "playlists"
    assert rows[0]["id"] == "P1" and rows[0]["name"] == "new songs" and rows[0]["kind"] == "inbox"


SONG_COLS_ROW = {
    "id": "A",
    "liked": 0,
    "liked_at": None,
    "first_seen": "t",
    "spotify_ids": '["x"]',
    "spotify_playable": 1,
    "first_year": 2010,
    "deezer_genres": "[]",
    "mb_tags": "[]",
    "title": "t",
    "artists": '["a"]',
    "deleted_at": None,
}
PLAYLIST_ROW = {
    "id": "S",
    "name": "My Shazam Tracks",
    "kind": "curated",
    "rule": None,
    "description": None,
    "snapshot_id": None,
    "pinned": 1,
    "expires_at": None,
    "deleted_at": None,
}
MEMBER_ROW = {
    "id": "S:A",
    "playlist_id": "S",
    "isrc": "A",
    "spotify_track_id": "x",
    "added_at": "t",
    "deleted_at": None,
}


def test_step_shazam_edges_pushes_one_edge_per_membership():
    hub = FakeHub(
        {
            "songs": [SONG_COLS_ROW],
            "playlists": [PLAYLIST_ROW],
            "playlist_songs": [MEMBER_ROW],
            "provenance": [],
        }
    )
    migrate.step_shazam_edges(hub, dry_run=False)
    table, rows = hub.pushed[0]
    assert table == "provenance"
    assert rows == [
        {
            "id": "shazam:A:A",
            "from_kind": "shazam",
            "from_ref": "A",
            "to_kind": "songs",
            "to_ref": "A",
            "rel": "imported_from",
            "asserted_by": "music-sync",
            "detail": {"created_row": 0},
        }
    ]


def test_step_shazam_edges_dry_run_pushes_nothing():
    hub = FakeHub(
        {
            "songs": [SONG_COLS_ROW],
            "playlists": [PLAYLIST_ROW],
            "playlist_songs": [MEMBER_ROW],
            "provenance": [],
        }
    )
    migrate.step_shazam_edges(hub, dry_run=True)
    assert hub.pushed == []


def test_step_like_pool_refuses_without_yes():
    hub = FakeHub()
    calls = []

    class FakeSpotify:
        def like(self, uris):
            calls.append(uris)

    migrate.step_like_pool(hub, FakeSpotify(), yes=False, dry_run=False)
    assert calls == []


def test_step_like_pool_likes_unliked_pool_songs():
    good = {**PLAYLIST_ROW, "id": "G", "name": "the good stuff"}
    member = {**MEMBER_ROW, "id": "G:A", "playlist_id": "G"}
    hub = FakeHub(
        {
            "songs": [SONG_COLS_ROW],
            "playlists": [good],
            "playlist_songs": [member],
            "provenance": [],
        }
    )
    calls = []

    class FakeSpotify:
        def like(self, uris):
            calls.append(uris)

    migrate.step_like_pool(hub, FakeSpotify(), yes=True, dry_run=False)
    assert calls == [["spotify:track:x"]]


def test_step_smart_buckets_sets_kind_and_rule():
    rows = [
        {**PLAYLIST_ROW, "id": "G", "name": "the good stuff"},
        {**PLAYLIST_ROW, "id": "X", "name": "galaxy"},
        {**PLAYLIST_ROW, "id": "R", "name": "rap"},
    ]
    hub = FakeHub({"songs": [], "playlists": rows, "playlist_songs": [], "provenance": []})
    migrate.step_smart_buckets(hub, dry_run=False)
    table, pushed = hub.pushed[0]
    by_id = {r["id"]: r for r in pushed}
    assert table == "playlists"
    assert by_id["G"]["kind"] == "smart" and by_id["G"]["rule"]["first_year"] == {"gte": 2000}
    assert by_id["X"]["rule"]["first_year"] == {"lt": 2000}
    assert by_id["R"]["rule"]["deezer_genres_any"] == ["Rap/Hip Hop"]
