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
