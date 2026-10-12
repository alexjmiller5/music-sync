from core import new_songs
from core.model import Song

T1, T2, T3, T4 = (f"2026-10-0{d}T00:00:00.000Z" for d in (1, 2, 3, 4))
T0 = "2026-09-30T00:00:00.000Z"


def song(isrc, **kw):
    return Song(isrc, kw.pop("liked", 0), kw.pop("liked_at", None), T1, **kw)


def test_a_song_comes_your_way_at_its_latest_shazam_like_or_find():
    assert new_songs.came_at(song("A")) is None
    assert new_songs.came_at(song("A", shazam_last_at=T2)) == T2
    assert new_songs.came_at(song("A", liked=1, liked_at=T3, shazam_last_at=T2)) == T3
    # An unliked song's old like time no longer counts.
    assert new_songs.came_at(song("A", liked=0, liked_at=T3, shazam_last_at=T2)) == T2
    assert new_songs.came_at(song("A", found_at=T4, shazam_last_at=T2)) == T4
    # Offsets compare by instant, not text.
    assert new_songs.came_at(
        song("A", found_at="2026-10-03T22:00:00-04:00", liked=1, liked_at=T3)
    ) == ("2026-10-03T22:00:00-04:00")


def test_ranking_interleaves_likes_and_shazams_newest_first_and_caps():
    songs = {
        "LIKED": song("LIKED", liked=1, liked_at=T2),
        "SHAZAM": song("SHAZAM", shazam_last_at=T3),
        "FOUND": song("FOUND", found_at=T1),
        "NONE": song("NONE"),
    }
    assert new_songs.rank(songs, cap=10) == ["SHAZAM", "LIKED", "FOUND"]
    assert new_songs.rank(songs, cap=2) == ["SHAZAM", "LIKED"]


def test_a_song_removed_from_new_songs_stays_out_until_it_comes_your_way_again():
    songs = {"A": song("A", shazam_last_at=T2), "B": song("B", shazam_last_at=T1)}
    assert new_songs.rank(songs, cap=10, dismissed={"A": T3}) == ["B"]
    songs["A"].liked, songs["A"].liked_at = 1, T4  # liked after it was removed
    assert new_songs.rank(songs, cap=10, dismissed={"A": T3}) == ["A", "B"]


def apply(current, ops):
    out = list(current)
    for op in ops:
        if op[0] == "remove":
            out = [u for u in out if u not in op[1]]
        elif op[0] == "move":
            _, start, before = op
            out.insert(before, out.pop(start))
        elif op[0] == "add":
            _, uri, position = op
            out.insert(position, uri)
    return out


def test_edits_reach_the_desired_order_with_few_operations():
    current = ["b", "c", "d", "x"]
    desired = ["a", "b", "c", "d"]  # a new song on top, the oldest falls off
    ops = new_songs.edits(current, desired)
    assert apply(current, ops) == desired
    assert ops == [("remove", ["x"]), ("add", "a", 0)]
    # A song that came your way again moves up, nothing else changes.
    ops = new_songs.edits(["a", "b", "c"], ["c", "a", "b"])
    assert ops == [("move", 2, 0)] and apply(["a", "b", "c"], ops) == ["c", "a", "b"]
    # A song placed by its listening time lands in the middle.
    ops = new_songs.edits(["a", "c"], ["a", "b", "c"])
    assert ops == [("add", "b", 1)]
    assert new_songs.edits(["a", "b"], ["a", "b"]) == []


def test_edits_reach_any_order():
    import itertools

    for current in itertools.permutations("abcd"):
        for desired in (list("dcba"), list("eadb"), list("e"), []):
            assert apply(list(current), new_songs.edits(list(current), desired)) == desired


def test_history_finds_songs_you_first_added_to_a_curated_playlist_or_new_songs():
    from core.model import Membership, Playlist

    playlists = {
        "CU": Playlist("CU", "mood", "curated", None, None, None, 1, None),
        "IN": Playlist("IN", "new songs", "inbox", None, None, None, 1, None),
        "SM": Playlist("SM", "pop", "smart", None, None, None, 1, None),
    }
    edges = [
        {"from_kind": "playlist", "from_ref": "CU", "to_ref": "A", "detail": {"created_row": 1}},
        {"from_kind": "playlist", "from_ref": "IN", "to_ref": "B", "detail": '{"created_row": 1}'},
        {"from_kind": "playlist", "from_ref": "SM", "to_ref": "C", "detail": {"created_row": 1}},
        {"from_kind": "playlist", "from_ref": "CU", "to_ref": "D", "detail": {"created_row": 0}},
        {"from_kind": "like", "from_ref": "liked", "to_ref": "E", "detail": {"created_row": 1}},
        {"from_kind": "playlist", "from_ref": "CU", "to_ref": "F", "detail": {"created_row": 1}},
    ]
    added = {
        ("CU", "A"): "2025-01-02T03:04:05Z",
        ("IN", "B"): "2026-10-01T00:00:00.000Z",
        ("SM", "C"): T1,
        ("CU", "D"): T1,
        ("CU", "F"): T1,
    }
    memberships = [Membership(p, i, "t", at, None) for (p, i), at in added.items()]
    # A was also added to a second curated playlist earlier, now removed: the
    # earliest curated add (Spotify's own date) wins.
    playlists["CU2"] = Playlist("CU2", "older", "curated", None, None, None, 1, None)
    memberships.append(Membership("CU2", "A", "t", "2024-06-01T00:00:00.000Z", T1))
    songs = {i: song(i) for i in "ABCDEF"}
    songs["F"].found_at = T0  # already set, earlier than its curated add: left alone
    songs["B"].found_at = "2026-10-05T00:00:00.000Z"  # later than its first add: moved back
    assert new_songs.found_from_history(songs, playlists, memberships, edges) == {
        "A": "2024-06-01T00:00:00.000Z",
        "B": "2026-10-01T00:00:00.000Z",
    }


def test_a_like_of_a_song_already_in_a_curated_playlist_is_not_when_it_came_your_way():
    # Music Sync likes curated songs itself (and the rollout liked them in bulk):
    # such a like is not a song coming your way; its curated add is.
    s = song("A", liked=1, liked_at=T3, shazam_last_at=T1)
    assert new_songs.came_at(s, curated_at=T2) == T1
    assert new_songs.came_at(s, curated_at=T4) == T3  # liked first, curated later
    assert new_songs.came_at(s) == T3
    songs = {"A": s, "B": song("B", shazam_last_at=T2)}
    assert new_songs.rank(songs, cap=10, curated_at={"A": T2}) == ["B", "A"]
