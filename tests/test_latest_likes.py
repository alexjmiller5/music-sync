import pytest
from core import rules
from core.model import Mirror, Playlist, Song

T1 = "2026-01-01T00:00:00.000Z"
T2 = "2026-01-02T00:00:00.000Z"


def mirror(rule):
    songs = {
        i: Song(i, liked, stamp, T1, first_year=year)
        for i, liked, stamp, year in [
            ("A", 1, T1, 1990),
            ("B", 1, T2, 1991),
            ("C", 1, T2, 2001),
            ("N", 1, None, 1990),
            ("U", 0, "2026-01-03T00:00:00.000Z", 1990),
        ]
    }
    p = Playlist("R", "Recent", "smart", rule, None, None, 1, None)
    return Mirror(songs, {"R": p}, {}, [], set())


def test_cap_uses_true_like_dates_and_stable_ties():
    m = mirror({"v": 1, "latest_likes": 2})
    assert rules.evaluate(m, {})["R"] == {"B", "C"}
    assert rules.evaluate(m, {})["R"] == {"B", "C"}
    m.playlists["R"].rule["latest_likes"] = 1
    assert rules.evaluate(m, {})["R"] == {"C"}


def test_cap_applies_after_the_rules_filter():
    m = mirror({"v": 1, "first_year": {"lt": 2000}, "latest_likes": 2})
    assert rules.evaluate(m, {})["R"] == {"A", "B"}


def test_reference_reuses_the_capped_set_and_not_its_uncapped_predicate():
    m = mirror({"v": 1, "latest_likes": 2})
    m.playlists["S"] = Playlist(
        "S", "Reference", "smart", {"v": 1, "matches_rule_ids_any": ["R"]}, None, None, 1, None
    )
    m.playlists["X"] = Playlist(
        "X", "Exclusion", "smart", {"v": 1, "not_matches_rule_ids": ["R"]}, None, None, 1, None
    )
    got = rules.evaluate(m, {})
    assert got["S"] == {"B", "C"}
    assert got["X"] == {"A", "N"}


def test_latest_like_order_ignores_later_shazam_and_curated_like_suppression():
    m = mirror({"v": 1, "latest_likes": 3})
    m.songs["A"].shazam_last_at = "2026-12-01T00:00:00.000Z"
    selected = {i: m.songs[i] for i in ["A", "B", "C"]}
    assert rules.order(selected, m.playlists["R"].rule, {"B": T1}) == ["C", "B", "A"]
    assert rules.order(selected, {"v": 1}, {}) == ["A", "C", "B"]


def test_description_names_the_selection():
    assert "latest 2 likes" in rules.describe({"v": 1, "latest_likes": 2}, "d")


@pytest.mark.parametrize("bad", [None, True, False, 0, -1, 1.5, "2"])
def test_limit_is_a_positive_integer(bad):
    with pytest.raises(rules.RuleError, match="latest_likes"):
        rules.validate({"v": 1, "latest_likes": bad})


def test_offset_dates_sort_by_instant_and_short_pools_remain_short():
    m = mirror({"v": 1, "latest_likes": 10})
    m.songs["B"].liked_at = "2026-01-02T01:00:00+01:00"
    assert rules.evaluate(m, {})["R"] == {"A", "B", "C"}
    assert rules.order({i: m.songs[i] for i in ["A", "B", "C"]}, m.playlists["R"].rule) == [
        "C",
        "B",
        "A",
    ]
    m.playlists["R"].rule["latest_likes"] = 1
    assert rules.evaluate(m, {})["R"] == {"C"}


def test_reconcile_plans_the_exact_capped_set_in_like_order():
    from datetime import datetime, timezone
    from core import reconcile
    from core.model import Live
    from tests.test_reconcile import item, live_pl

    m = mirror({"v": 1, "latest_likes": 2})
    m.songs = {i: m.songs[i] for i in ["A", "B", "C"]}
    m.songs["B"].shazam_last_at = "2026-12-01T00:00:00.000Z"
    live = Live(
        {"R": live_pl("R", "Recent", [item("A"), item("B")])},
        {i: item(i, added=s.liked_at) for i, s in m.songs.items()},
        {},
    )
    actions = reconcile.plan(m, live, datetime(2026, 9, 8, tzinfo=timezone.utc))
    assert [a.isrc for a in actions if a.kind == "add_item"] == ["C"]
    assert [a.isrc for a in actions if a.kind == "remove_item"] == ["A"]
    arranged = [a for a in actions if a.kind == "arrange_items"]
    assert len(arranged) == 1
    assert arranged[0].uris == ["spotify:track:tC", "spotify:track:tB"]


def test_order_keeps_untimed_songs_after_dated_songs():
    m = mirror({"v": 1, "latest_likes": 2})
    assert rules.order(m.songs, m.playlists["R"].rule) == ["U", "C", "B", "A", "N"]
