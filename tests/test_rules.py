import pytest

from core import rules
from core.model import Membership, Mirror, Playlist, Song

IDS = {"feel good": "PF", "😴": "PS"}


def song(isrc, liked=1, year=None, genres=(), tags=(), liked_at=None):
    return Song(isrc, liked, liked_at, "t", ["x"], 1, year, list(genres), list(tags), "t", ["a"])


def test_validate_accepts_full_rule():
    rules.validate(
        {
            "v": 1,
            "deezer_genres_any": ["Rap/Hip Hop"],
            "mb_tags_any": ["house"],
            "first_year": {"gte": 1990, "lt": 2000},
            "in_playlist_any": ["feel good"],
            "not_in_playlist": ["😴"],
            "captured_by": "shazam",
            "liked_after": "2025-01-01",
        }
    )


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"v": 2},
        {"v": 1, "genre": ["x"]},
        {"v": 1, "deezer_genres_any": "Pop"},
        {"v": 1, "first_year": {"eq": 1999}},
        {"v": 1, "first_year": {"between": [1999]}},
        {"v": 1, "captured_by": "radio"},
        {"v": 1, "liked_after": "yesterday"},
        {"v": 1, "mb_tags_any": []},
    ],
)
def test_validate_rejects(bad):
    with pytest.raises(rules.RuleError):
        rules.validate(bad)


def test_to_sql_unknown_playlist_name():
    with pytest.raises(rules.RuleError, match="unknown playlist"):
        rules.to_sql({"v": 1, "in_playlist_any": ["nope"]}, IDS)


def test_describe():
    r = {
        "v": 1,
        "deezer_genres_any": ["Rap/Hip Hop"],
        "first_year": {"lt": 2000},
        "in_playlist_any": ["feel good"],
        "not_in_playlist": ["😴"],
        "captured_by": "shazam",
        "liked_after": "2025-01-01",
    }
    assert rules.describe(r, "2026-09-08") == (
        "smart · genre: Rap/Hip Hop · year < 2000 · in: feel good · not in: 😴 · shazamed · liked after 2025-01-01 · synced 2026-09-08"
    )
    assert (
        rules.describe({"v": 1, "first_year": {"between": [1990, 1999]}}, "d")
        == "smart · year 1990-1999 · synced d"
    )
    assert len(rules.describe({"v": 1, "mb_tags_any": ["x" * 400]}, "d")) == 300


def make_mirror():
    songs = {
        "A": song("A", year=1995, genres=["Rap/Hip Hop"]),
        "B": song("B", year=2005, genres=["Rap/Hip Hop"]),
        "C": song("C", liked=0, year=1990, genres=["Rap/Hip Hop"]),
        "D": song(
            "D", year=1980, tags=["deep house", "house"], liked_at="2025-06-01T00:00:00.000Z"
        ),
        "E": song("E", year=1980, tags=["house"], liked_at="2024-06-01T00:00:00.000Z"),
    }
    playlists = {
        "PF": Playlist("PF", "feel good", "curated", None, None, None, 1, None),
        "PS": Playlist("PS", "😴", "curated", None, None, None, 1, None),
        "R": Playlist(
            "R",
            "rap 90s",
            "smart",
            {"v": 1, "deezer_genres_any": ["Rap/Hip Hop"], "first_year": {"lt": 2000}},
            None,
            None,
            1,
            None,
        ),
        "H": Playlist(
            "H",
            "house",
            "smart",
            {
                "v": 1,
                "mb_tags_any": ["house"],
                "not_in_playlist": ["😴"],
                "liked_after": "2025-01-01",
            },
            None,
            None,
            1,
            None,
        ),
        "F": Playlist(
            "F",
            "feel shaz",
            "smart",
            {"v": 1, "in_playlist_any": ["feel good"], "captured_by": "shazam"},
            None,
            None,
            1,
            None,
        ),
    }
    memberships = {
        ("PF", "A"): Membership("PF", "A", "x", "t"),
        ("PS", "E"): Membership("PS", "E", "x", "t"),
        ("PF", "D"): Membership("PF", "D", "x", "t"),
    }
    return Mirror(songs, playlists, memberships, [], {("A", "shazam"), ("D", "like")})


def test_evaluate_all_predicates():
    out = rules.evaluate(make_mirror(), {"feel good": "PF", "😴": "PS"})
    assert out == {"R": {"A"}, "H": {"D"}, "F": {"A"}}


def test_evaluate_records_error_and_skips_playlist_when_errors_dict_given():
    m = make_mirror()
    m.playlists["BAD"] = Playlist(
        "BAD", "bad rule", "smart", {"v": 1, "in_playlist_any": ["gone"]}, None, None, 1, None
    )
    errors: dict[str, str] = {}
    out = rules.evaluate(m, {"feel good": "PF", "😴": "PS"}, errors)
    assert "BAD" not in out and "unknown playlist" in errors["BAD"]
    assert out == {"R": {"A"}, "H": {"D"}, "F": {"A"}}


def test_evaluate_raises_without_errors_dict():
    m = make_mirror()
    m.playlists["BAD"] = Playlist(
        "BAD", "bad rule", "smart", {"v": 1, "in_playlist_any": ["gone"]}, None, None, 1, None
    )
    with pytest.raises(rules.RuleError):
        rules.evaluate(m, {"feel good": "PF", "😴": "PS"})


def test_check_sql_lists_mismatches_in_sqlite():
    m = make_mirror()
    m.memberships[("R", "B")] = Membership("R", "B", "x", "t")  # B is 2005: violates rap 90s
    m.memberships[("R", "A")] = Membership("R", "A", "x", "t")
    db = rules.load_sqlite(m)
    sql = rules.check_sql(
        [p for p in m.playlists.values() if p.kind == "smart"], {"feel good": "PF", "😴": "PS"}
    )
    assert {r[0] for r in db.execute(sql)} == {"R:B"}
