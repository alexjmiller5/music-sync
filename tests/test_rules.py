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


def test_stable_playlist_references_survive_renames_and_duplicate_names():
    m = make_mirror()
    m.playlists["F"].rule = {"v": 1, "in_playlist_ids_any": ["PF"], "not_in_playlist_ids": ["PS"]}
    m.playlists["PF"].name = m.playlists["PS"].name = "Renamed"
    assert rules.evaluate(m, {}, {})["F"] == {"A", "D"}
    m.playlists["F"].rule["in_playlist_ids_any"] = ["missing"]
    errors = {}
    assert "F" not in rules.evaluate(m, {}, errors)
    assert "unknown playlist ID" in errors["F"]


def test_ambiguous_legacy_playlist_names_are_held_for_review():
    m = make_mirror()
    m.playlists["PS"].name = "feel good"
    errors = {}
    out = rules.evaluate(m, {"feel good": "PF"}, errors)
    assert "F" not in out
    assert "ambiguous playlist" in errors["F"]


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


@pytest.mark.parametrize(
    "rule",
    [
        {"v": 1, "captured_by": []},
        {"v": 1, "first_year": {"gte": True}},
        {"v": 1, "first_year": {"between": [2020, 1990]}},
    ],
)
def test_malformed_rule_is_a_review_error(rule):
    with pytest.raises(rules.RuleError):
        rules.validate(rule)


def test_missing_smart_rule_is_reported_instead_of_silently_skipped():
    from core.model import Mirror, Playlist

    m = Mirror({}, {"P": Playlist("P", "Empty", "smart", None, None, None, 1, None)}, {}, [], set())
    errors = {}
    assert rules.evaluate(m, {}, errors) == {}
    assert "P" in errors


# --- one genre predicate shared by several smart playlists ---------------------------

GENRE = {"v": 1, "genre_any": {"deezer": ["Rap/Hip Hop"]}}
WITH_TAGS = {
    "v": 1,
    "genre_any": {"deezer": ["Rap/Hip Hop"], "mb_tags_contain": ["rap", "hip hop"]},
}


def buckets(genre_rule):
    songs = {
        "R90": song("R90", year=1995, genres=["Rap/Hip Hop"]),
        "R05": song("R05", year=2005, genres=["Pop", "Rap/Hip Hop"]),
        "P90": song("P90", year=1990, genres=["Rock"]),
        "P10": song("P10", year=2010, genres=["Pop"]),
        "U01": song("U01", year=2001),  # unknown genre is not the genre
        "T03": song("T03", year=2003, genres=["Pop"], tags=["Pop Rap"]),
        "T98": song("T98", year=1998, tags=["east coast hip hop"]),
        "OFF": song("OFF", liked=0, year=2004, genres=["Rap/Hip Hop"]),
        "NOY": song("NOY", genres=["Rap/Hip Hop"]),
    }

    def smart(pid, rule):
        return Playlist(pid, pid.lower(), "smart", rule, None, None, 1, None)

    playlists = {
        "RAP": smart("RAP", genre_rule),
        "GOOD": smart(
            "GOOD", {"v": 1, "first_year": {"gte": 2000}, "not_matches_rule_ids": ["RAP"]}
        ),
        "GAL": smart("GAL", {"v": 1, "first_year": {"lt": 2000}, "not_matches_rule_ids": ["RAP"]}),
    }
    return Mirror(songs, playlists, {}, [], set())


def test_one_genre_predicate_drives_all_three_rules():
    out = rules.evaluate(buckets(GENRE), {})
    assert out == {
        "RAP": {"R90", "R05", "NOY"},
        "GOOD": {"P10", "U01", "T03"},
        "GAL": {"P90", "T98"},
    }


def test_musicbrainz_tag_option_changes_only_the_shared_predicate():
    out = rules.evaluate(buckets(WITH_TAGS), {})
    assert out == {
        "RAP": {"R90", "R05", "NOY", "T03", "T98"},
        "GOOD": {"P10", "U01"},
        "GAL": {"P90"},
    }


def test_shared_predicate_reference_errors():
    m = buckets(GENRE)
    m.playlists["GOOD"].rule = {"v": 1, "not_matches_rule_ids": ["NOPE"]}
    m.playlists["GAL"].rule = {"v": 1, "matches_rule_ids_any": ["GAL"]}
    m.playlists["CUR"] = Playlist("CUR", "cur", "curated", None, None, None, 1, None)
    m.playlists["X"] = Playlist(
        "X", "x", "smart", {"v": 1, "matches_rule_ids_any": ["CUR"]}, None, None, 1, None
    )
    errors = {}
    out = rules.evaluate(m, {}, errors)
    assert set(out) == {"RAP"}
    assert "unknown playlist ID" in errors["GOOD"]
    assert "cycle" in errors["GAL"]
    assert "not a smart playlist" in errors["X"]


@pytest.mark.parametrize(
    "bad",
    [
        {"v": 1, "genre_any": {}},
        {"v": 1, "genre_any": ["Rap/Hip Hop"]},
        {"v": 1, "genre_any": {"deezer": []}},
        {"v": 1, "genre_any": {"deezer": ["x"], "spotify": ["y"]}},
        {"v": 1, "genre_any": {"mb_tags_contain": [""]}},
        {"v": 1, "not_matches_rule_ids": []},
        {"v": 1, "matches_rule_ids_any": "RAP"},
    ],
)
def test_shared_predicate_validation(bad):
    with pytest.raises(rules.RuleError):
        rules.validate(bad)


def test_describe_shared_predicate():
    assert rules.describe(GENRE, "d") == "smart · genre: Rap/Hip Hop · synced d"
    assert (
        rules.describe(WITH_TAGS, "d")
        == "smart · genre: Rap/Hip Hop or tags containing rap, hip hop · synced d"
    )
    good = {"v": 1, "first_year": {"gte": 2000}, "not_matches_rule_ids": ["RAP"]}
    assert rules.describe(good, "d", {"RAP": "rap"}) == "smart · not: rap · year >= 2000 · synced d"
    assert (
        rules.describe({"v": 1, "matches_rule_ids_any": ["RAP"]}, "d")
        == "smart · matches: RAP · synced d"
    )
