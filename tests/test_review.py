import importlib.util
import sys
from pathlib import Path

from core.model import Live, LiveItem, LivePlaylist, Membership, Mirror, Playlist, Song

spec = importlib.util.spec_from_file_location(
    "review", Path(__file__).parents[1] / "scripts" / "review.py"
)
review = importlib.util.module_from_spec(spec)
sys.modules["review"] = review
spec.loader.exec_module(review)


def song(
    isrc, liked=1, year=2010, genres=("Pop",), title="t", artists=("a",), ids=("x",), playable=1
):
    return Song(
        isrc, liked, None, "f", list(ids), playable, year, list(genres), [], title, list(artists)
    )


def test_report_sections():
    songs = {
        "A": song("A", liked=0),
        "B": song("B"),
        "C": song("C", year=1995),
        "D": song("D", title="Same", artists=["X"]),
        "E": song("E", title="Same", artists=["X"]),
        "F": song("F", playable=0, ids=[]),
    }
    playlists = {
        "G": Playlist("G", "the good stuff", "curated", None, None, None, 1, None),
        "S": Playlist("S", "My Shazam Tracks", "curated", None, None, None, 1, None),
    }
    memberships = {
        ("G", "A"): Membership("G", "A", "x", "t"),
        ("G", "C"): Membership("G", "C", "x", "t"),
        ("S", "A"): Membership("S", "A", "x", "t"),
        ("G", "F"): Membership("G", "F", "x", "t"),
    }
    m = Mirror(songs, playlists, memberships, [], set())
    live = Live(
        {
            "G": LivePlaylist(
                "G",
                "the good stuff",
                None,
                "s",
                [
                    LiveItem("F", "x", "spotify:track:x", "t", False, False, "t", ["a"]),
                    LiveItem(None, None, "spotify:local:y", "t", False, True, "local", []),
                    LiveItem("C", "x", "spotify:track:x", "t", True, False, "t", ["a"]),
                    LiveItem("C", "x2", "spotify:track:x2", "t2", True, False, "t", ["a"]),
                ],
            )
        },
        {},
        {},
    )
    r = review.report(m, live)
    assert [ln for ln in r["curated_not_liked"] if "[A]" in ln]
    assert [ln for ln in r["liked_no_playlist"] if "[B]" in ln] and [
        ln for ln in r["liked_no_playlist"] if "[D]" in ln
    ]
    assert [ln for ln in r["bucket_mismatch"] if "[C]" in ln and "the good stuff" in ln]
    assert [ln for ln in r["unplayable_none"] if "[F]" in ln]
    assert [ln for ln in r["dup_isrc_in_playlist"] if "[C]" in ln]
    assert [ln for ln in r["same_title_diff_isrc"] if "Same" in ln]
    assert [ln for ln in r["shazam_never_liked"] if "[A]" in ln]
    assert r["no_isrc"] == ["the good stuff: local (local file)"]
