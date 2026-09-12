from datetime import datetime, timezone
import gzip
import json
from copy import deepcopy

import pytest

from core import capture


@pytest.fixture(autouse=True)
def archive_io(mocker):
    mocker.patch("core.archive.get", return_value=None)
    mocker.patch("core.archive.put")


NOW = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)


def track(tid, name, artist, isrc="USUM71703861", playable=True):
    return {
        "id": tid,
        "uri": f"spotify:track:{tid}",
        "name": name,
        "is_playable": playable,
        "artists": [{"name": artist}],
        "external_ids": {"isrc": isrc},
        "album": {"name": "a", "release_date": "2017-01-01"},
    }


def test_best_match_normalizes_title_and_artist():
    tracks = [
        track("1", "Money Trees", "Kendrick Lamar"),
        track("2", "Money Trees - Live", "Kendrick Lamar"),
    ]
    assert capture.best_match("money trees", "Kendrick Lamar", tracks)["id"] == "1"
    assert capture.best_match("Nope", "Kendrick Lamar", tracks) is None


def test_best_match_preserves_version_suffixes():
    tracks = [
        track("1", "T-Shirt", "Migos"),
        track("2", "T", "Migos"),
    ]
    assert capture.best_match("T-Shirt", "Migos", tracks)["id"] == "1"
    tracks = [
        track("2", "Money Trees - Live", "Kendrick Lamar"),
    ]
    assert capture.best_match("Money Trees", "Kendrick Lamar", tracks) is None


def test_best_match_prefers_the_exact_release_over_a_live_version():
    tracks = [
        track("live", "Song - Live", "Artist"),
        track("studio", "Song", "Artist"),
    ]

    assert capture.best_match("Song", "Artist", tracks)["id"] == "studio"


@pytest.mark.parametrize(
    "title,artist,tracks",
    [
        ("Song - Remix", "Artist", [track("studio", "Song", "Artist")]),
        ("Song", "Artist", [track("unrelated", "Song", "Unrelated")]),
    ],
)
def test_best_match_rejects_wrong_version_and_artist(title, artist, tracks):
    assert capture.best_match(title, artist, tracks) is None


class FakeSpotify:
    def __init__(self, tracks, inbox_items=()):
        self.tracks, self.inbox_items, self.calls, self.searches = (
            tracks,
            list(inbox_items),
            [],
            [],
        )

    def search_track(self, title, artist, market):
        self.searches.append(("metadata", title, artist, market))
        return self.tracks

    def search_isrc(self, isrc, market):
        self.searches.append(("isrc", isrc, market))
        return self.tracks

    def add_items(self, pid, uris):
        self.calls.append(("add", pid, uris))
        # Add the matched track to inbox_items so it shows up in get_playlist_items
        for uri in uris:
            for t in self.tracks:
                if t["uri"] == uri:
                    self.inbox_items.append(
                        {
                            "added_at": "2026-09-08T12:00:00.000Z",
                            "item": t,
                        }
                    )
                    break

    def remove_items(self, pid, uris):
        self.calls.append(("remove", pid, uris))

    def get_playlist_items(self, pid, market):
        return self.inbox_items


class FakeHub:
    def catalog(self):
        from tests.test_metadata_contract import catalog

        return catalog()

    def __init__(self, playlists, songs=(), memberships=()):
        self.tables = {
            "playlists": playlists,
            "songs": list(songs),
            "playlist_songs": list(memberships),
            "provenance": [],
        }
        self.pushed = []

    def pull(self, table, columns, since=""):
        return [{c: r.get(c) for c in columns} for r in self.tables[table]]

    def push(self, table, rows):
        self.pushed.append((table, rows))
        return {"upserted": len(rows), "rejected": []}


INBOX = [
    {
        "id": "IN",
        "name": "new songs",
        "kind": "inbox",
        "rule": None,
        "description": None,
        "snapshot_id": None,
        "pinned": 1,
        "expires_at": None,
        "deleted_at": None,
    }
]


def test_resolve_track_uses_isrc_and_requires_candidate_isrc(settings):
    matching = track("match", "Different metadata", "Someone", isrc="USAAA2600001")
    spotify = FakeSpotify([matching])
    payload = {"title": "Song", "artist": "Artist", "isrc": "USAAA2600001"}

    assert capture.resolve_track(payload, spotify, settings) == matching
    assert spotify.searches == [("isrc", "USAAA2600001", "US")]

    spotify = FakeSpotify([track("wrong", "Song", "Artist", isrc="USAAA2600002")])
    assert capture.resolve_track(payload, spotify, settings) is None
    assert spotify.searches == [("isrc", "USAAA2600001", "US")]


def test_capture_adds_new_song_with_shazam_edge(settings):
    sp, hub = FakeSpotify([track("1", "Money Trees", "Kendrick Lamar")]), FakeHub(INBOX)
    out = capture.capture(
        {
            "title": "Money Trees",
            "artist": "Kendrick Lamar",
            "apple_music_id": "12",
            "shazam_url": "https://s",
        },
        sp,
        hub,
        settings,
        NOW,
    )
    assert out == {
        "ok": True,
        "message": "Money Trees by Kendrick Lamar added to new songs",
        "isrc": "USUM71703861",
    }
    assert ("add", "IN", ["spotify:track:1"]) in sp.calls
    tables = dict(hub.pushed)
    assert tables["songs"][0]["id"] == "USUM71703861" and tables["songs"][0]["liked"] == 0
    assert tables["playlist_songs"][0]["id"] == "IN:USUM71703861"
    edge = tables["provenance"][0]
    assert (
        edge["id"] == "shazam:12:USUM71703861"
        and edge["from_kind"] == "shazam"
        and edge["detail"]["created_row"] == 1
    )


def test_capture_already_in_inbox(settings):
    hub = FakeHub(
        INBOX,
        songs=[{"id": "USUM71703861", "liked": 0, "deleted_at": None}],
        memberships=[
            {
                "id": "IN:USUM71703861",
                "playlist_id": "IN",
                "isrc": "USUM71703861",
                "spotify_track_id": "1",
                "added_at": "t",
                "deleted_at": None,
            }
        ],
    )
    tr = track("1", "Money Trees", "Kendrick Lamar")
    sp = FakeSpotify([tr], inbox_items=[{"added_at": "t", "item": tr}])
    out = capture.capture(
        {"title": "Money Trees", "artist": "Kendrick Lamar"}, sp, hub, settings, NOW
    )
    assert out["ok"] and "already" in out["message"] and sp.calls == []


def test_capture_no_match_and_missing_fields(settings):
    sp, hub = FakeSpotify([]), FakeHub(INBOX)
    assert capture.capture({"title": "X", "artist": "Y"}, sp, hub, settings, NOW) == {
        "ok": False,
        "message": "Could not find X by Y on Spotify",
        "isrc": None,
    }
    assert (
        capture.capture({"title": "X"}, sp, hub, settings, NOW)["message"]
        == "title and artist required"
    )


def test_capture_trims_inbox(settings):
    items = [
        {
            "added_at": f"2026-08-{i + 1:02d}T00:00:00.000Z",
            "item": track(str(i), "n", "a", isrc=f"US{i:010d}"[:12]),
        }
        for i in range(3)
    ]
    sp, hub = (
        FakeSpotify([track("9", "New", "A", isrc="GBBTV1101287")], inbox_items=items),
        FakeHub(INBOX),
    )
    settings.inbox_cap = 2
    capture.capture({"title": "New", "artist": "A"}, sp, hub, settings, NOW)
    # With 3 existing items + 1 new = 4 total, inbox_cap=2 removes oldest 2
    assert ("remove", "IN", ["spotify:track:0", "spotify:track:1"]) in sp.calls
    # The new song (track:9) must not be in any remove call
    for call in sp.calls:
        if call[0] == "remove":
            assert "spotify:track:9" not in call[2]


@pytest.mark.parametrize("known", [False, True])
def test_capture_first_write_keeps_resolved_and_expiring_metadata(settings, mocker, known):
    tr = track("resolved", "Title", "Artist", isrc="USAAA2600001")
    tr.update(duration_ms=123456, linked_from={"id": "original"})
    old = {
        "added_at": "2020-01-01T00:00:00Z",
        "track": track("old", "Old title", "Old artist", isrc="USAAA2600002"),
    }
    sp = FakeSpotify([tr], [old])
    hub = FakeHub(INBOX, songs=[{"id": "USAAA2600001", "liked": 1}] if known else [])
    settings.inbox_cap = 1
    saved = {}
    before = deepcopy(sp.inbox_items)

    def archive(settings, key, data):
        assert sp.calls == [] and hub.pushed == []
        saved[key] = json.loads(gzip.decompress(data))

    mocker.patch("core.archive.put", side_effect=archive)
    out = capture.capture({"title": "Title", "artist": "Artist"}, sp, hub, settings, NOW)
    assert out["ok"]
    source, body = next(iter(saved.items()))
    assert body == {"playlist_id": "IN", "items": before, "resolved_track": tr}
    songs = [r for table, rows in hub.pushed if table == "songs" for r in rows]
    resolved = next(r for r in songs if r["id"] == "USAAA2600001")
    assert resolved["title"] == "Title" and resolved["duration_ms"] == 123456
    assert resolved["album"] == "a" and resolved["album_year"] == 2017
    assert resolved["spotify_ids"] == ["original", "resolved"]
    assert ("liked" in resolved) is (not known)
    expired = next(r for r in songs if r["id"] == "USAAA2600002")
    assert expired["title"] == "Old title" and expired["spotify_ids"] == ["old"]
    assert ("remove", "IN", ["spotify:track:old"]) in sp.calls
    edges = [r for table, rows in hub.pushed if table == "provenance" for r in rows]
    direct = [r for r in edges if r["rel"] == "evidence_of"]
    assert direct and all(r["from_ref"] == source for r in direct)
    assert all(r["detail"]["observed_at"] == "2026-09-08T12:00:00.000Z" for r in direct)
    origin = next(r for r in edges if r["from_kind"] == "shazam")
    assert origin["rel"] == "imported_from" and origin["detail"]["created_row"] == int(not known)


def test_known_capture_refreshes_metadata_even_when_already_in_inbox(settings):
    tr = track("observed", "Title", "Artist", isrc="USAAA2600001")
    sp = FakeSpotify([tr], [{"added_at": "2020-01-01T00:00:00Z", "item": tr}])
    hub = FakeHub(INBOX, songs=[{"id": "USAAA2600001", "liked": 1, "title": "Stale title"}])
    assert capture.capture({"title": "Title", "artist": "Artist"}, sp, hub, settings, NOW)["ok"]
    assert sp.calls == []
    song = next(r for table, rows in hub.pushed if table == "songs" for r in rows)
    assert song["title"] == "Title" and "liked" not in song


def test_pending_replay_blocks_capture_before_search_or_hub_read(settings, monkeypatch):
    pending = gzip.compress(
        json.dumps(
            {
                "intent": "metadata_replay",
                "writes": False,
                "planned": [],
                "operations": [],
            }
        ).encode()
    )
    monkeypatch.setattr(capture.archive, "get", lambda *args: pending)
    out = capture.capture({"title": "Title", "artist": "Artist"}, object(), object(), settings, NOW)
    assert not out["ok"] and "recovery" in out["message"]
