"""Supported rule configuration path."""

import pytest

from core import smart
from core.model import Mirror, Playlist

TODAY = "2026-10-09"


class Hub:
    def __init__(self):
        self.pushed = []

    def push(self, table, rows):
        self.pushed.append((table, rows))


class Spotify:
    def __init__(self):
        self.created = []

    def create_playlist(self, name, description, public=False):
        self.created.append((name, description, public))
        return {"id": "NEW"}


def mirror():
    return Mirror(
        {},
        {
            "C": Playlist("C", "curated list", "curated", None, None, None, 1, None),
            "N": Playlist("N", "new songs", "inbox", None, None, None, 1, None),
        },
        {},
        [],
        set(),
    )


LIVE = {"C": "curated list", "N": "new songs", "U": "unclassified"}


def test_create_makes_a_private_smart_playlist_keyed_by_its_new_id():
    hub, spotify = Hub(), Spotify()
    rule = {"v": 1, "first_year": {"lt": 2000}, "not_in_playlist_ids": ["C"]}
    out = smart.configure(
        {"action": "create", "name": "older", "rule": rule},
        hub=hub,
        mirror=mirror(),
        live_names=LIVE,
        today=TODAY,
        spotify=spotify,
    )
    assert spotify.created == [("older", out["description"], False)]
    assert out["id"] == "NEW" and out["kind"] == "smart" and out["rule"] == rule
    assert "not in: curated list" in out["description"]
    assert hub.pushed == [("playlists", [out])]


@pytest.mark.parametrize(
    "body",
    [
        {"action": "create", "name": "curated list", "rule": {"v": 1}},
        {"action": "create", "name": "x", "rule": {"v": 1, "in_playlist_ids_any": ["GONE"]}},
        {"action": "create", "name": "x", "rule": {"v": 2}},
        {"action": "create", "name": "x", "rule": {"v": 1, "in_playlist_any": ["curated list"]}},
        {"action": "smart", "playlist_id": "MISSING", "rule": {"v": 1}},
        {"action": "clear", "playlist_id": "C"},
        {"action": "curated", "playlist_id": "N"},
        {"action": "delete", "playlist_id": "C"},
    ],
)
def test_invalid_configuration_writes_nothing(body):
    hub, spotify = Hub(), Spotify()
    with pytest.raises(smart.ConfigError):
        smart.configure(
            body, hub=hub, mirror=mirror(), live_names=LIVE, today=TODAY, spotify=spotify
        )
    assert hub.pushed == [] and spotify.created == []


def test_classify_and_convert_existing_playlists():
    hub = Hub()
    curated = smart.configure(
        {"action": "curated", "playlist_id": "U"},
        hub=hub,
        mirror=mirror(),
        live_names=LIVE,
        today=TODAY,
    )
    assert curated["kind"] == "curated" and curated["rule"] is None
    converted = smart.configure(
        {"action": "smart", "playlist_id": "C", "rule": {"v": 1, "liked_after": "2026-01-01"}},
        hub=hub,
        mirror=mirror(),
        live_names=LIVE,
        today=TODAY,
    )
    assert converted["kind"] == "smart" and converted["name"] == "curated list"


def mirror_with_shared_genre():
    m = mirror()
    m.playlists["RAP"] = Playlist(
        "RAP",
        "rap",
        "smart",
        {"v": 1, "genre_any": {"deezer": ["Rap/Hip Hop"]}},
        None,
        None,
        1,
        None,
    )
    m.playlists["GOOD"] = Playlist(
        "GOOD",
        "good",
        "smart",
        {"v": 1, "first_year": {"gte": 2000}, "not_matches_rule_ids": ["RAP"]},
        None,
        None,
        1,
        None,
    )
    return m


SHARED_LIVE = {**LIVE, "RAP": "rap", "GOOD": "good"}


def test_a_rule_can_reuse_another_smart_playlists_predicate():
    hub = Hub()
    out = smart.configure(
        {
            "action": "smart",
            "playlist_id": "C",
            "rule": {"v": 1, "first_year": {"lt": 2000}, "not_matches_rule_ids": ["RAP"]},
        },
        hub=hub,
        mirror=mirror_with_shared_genre(),
        live_names=SHARED_LIVE,
        today=TODAY,
    )
    assert "not: rap" in out["description"]
    # Changing the shared predicate once is one configuration change.
    tags = {"v": 1, "genre_any": {"deezer": ["Rap/Hip Hop"], "mb_tags_contain": ["rap"]}}
    out = smart.configure(
        {"action": "smart", "playlist_id": "RAP", "rule": tags},
        hub=hub,
        mirror=mirror_with_shared_genre(),
        live_names=SHARED_LIVE,
        today=TODAY,
    )
    assert out["rule"] == tags and len(hub.pushed) == 2


@pytest.mark.parametrize(
    "body",
    [
        {"action": "smart", "playlist_id": "U", "rule": {"v": 1, "matches_rule_ids_any": ["C"]}},
        {
            "action": "smart",
            "playlist_id": "RAP",
            "rule": {"v": 1, "matches_rule_ids_any": ["GOOD"]},
        },
        {"action": "clear", "playlist_id": "RAP"},
        {"action": "curated", "playlist_id": "RAP"},
    ],
)
def test_shared_predicate_cannot_dangle_or_cycle(body):
    hub = Hub()
    with pytest.raises(smart.ConfigError):
        smart.configure(
            body, hub=hub, mirror=mirror_with_shared_genre(), live_names=SHARED_LIVE, today=TODAY
        )
    assert hub.pushed == []
