from datetime import datetime, timezone

from core import capture

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
        track("1", "Money Trees (feat. Jay Rock)", "Kendrick Lamar"),
        track("2", "Money Trees - Live", "Kendrick Lamar"),
    ]
    assert capture.best_match("money trees", "Kendrick Lamar", tracks)["id"] == "1"
    assert capture.best_match("Nope", "Kendrick Lamar", tracks) is None


def test_best_match_hyphen_only_in_suffix():
    # T-Shirt should not strip at hyphen (no preceding space)
    tracks = [
        track("1", "T-Shirt", "Migos"),
        track("2", "T", "Migos"),
    ]
    assert capture.best_match("T-Shirt", "Migos", tracks)["id"] == "1"
    # " - Live" suffix should be stripped (hyphen preceded by space)
    tracks = [
        track("2", "Money Trees - Live", "Kendrick Lamar"),
    ]
    assert capture.best_match("Money Trees", "Kendrick Lamar", tracks)["id"] == "2"


class FakeSpotify:
    def __init__(self, tracks, inbox_items=()):
        self.tracks, self.inbox_items, self.calls = tracks, list(inbox_items), []

    def search_track(self, title, artist, market):
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
    sp = FakeSpotify([track("1", "Money Trees", "Kendrick Lamar")])
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
