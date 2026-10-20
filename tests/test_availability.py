"""A fresh Spotify pull must not erase unavailable recordings from the catalog."""

from datetime import datetime, timezone

import httpx
import pytest

from core import mirror, reconcile
from core.model import Membership, Mirror, Playlist, Song
from core.run import _curated

ISRC = "USAAA2600001"
STAMP = "2026-01-01T00:00:00.000Z"


def catalog(kind="curated"):
    return Mirror(
        {
            ISRC: Song(
                ISRC,
                1,
                STAMP,
                STAMP,
                ["old"],
                1,
                title="Song",
                artists=["Artist"],
                duration_ms=180000,
            )
        },
        {"P": Playlist("P", "Playlist", kind, None, None, "before", 1, None)},
        {("P", ISRC): Membership("P", ISRC, "old", STAMP)},
        [],
        set(),
    )


def track(playable=True, track_id="old", isrc=ISRC):
    return {
        "id": track_id,
        "uri": f"spotify:track:{track_id}",
        "is_playable": playable,
        "external_ids": {"isrc": isrc},
        "name": "Song",
        "artists": [{"name": "Artist"}],
        "duration_ms": 180000,
    }


class Spotify:
    def __init__(self, playable):
        self.playable = playable
        self.lookups = []

    def get_playlists(self):
        return [{"id": "P", "name": "Playlist", "owner": {"id": "owner"}, "snapshot_id": "after"}]

    def get_playlist_items(self, *args):
        return []

    def get_liked(self, *args):
        return []

    def search_isrc(self, *args):
        return []

    def get_track(self, tid, market):
        self.lookups.append(tid)
        if isinstance(self.playable, Exception):
            raise self.playable
        return track(self.playable)


@pytest.mark.parametrize("playable", [False, None])
@pytest.mark.parametrize("observation_only", [False, True])
def test_missing_unavailable_recording_preserves_likes_metadata_and_membership(
    playable, observation_only
):
    m = catalog()
    spotify = Spotify(playable)
    live = mirror.pull_live(spotify, "US", "owner", m, full=True)
    actions = reconcile.plan(
        m, live, datetime.now(timezone.utc), market="US", observation_only=observation_only
    )
    assert spotify.lookups == ["old"], "one lookup shared by missing likes and membership"
    assert _curated(m, live) == {("P", ISRC)}
    assert not [a for a in actions if a.kind == "delete_membership"]
    assert not [a for a in actions if a.kind == "upsert_song" and a.row.get("liked") == 0]
    assert any(a.kind == "flag" and "availability" in a.text.lower() for a in actions)
    assert m.songs[ISRC].title == "Song" and m.songs[ISRC].first_seen == STAMP
    # The retained raw observation reproduces the safety decision without another API read.
    replay = mirror.live_from_raw(live.raw, "owner", m)
    assert replay.raw == live.raw and set(replay.liked) == {ISRC}


def test_playable_recording_removed_by_user_is_not_preserved():
    m = catalog()
    live = mirror.pull_live(Spotify(True), "US", "owner", m, full=True)
    actions = reconcile.plan(m, live, datetime.now(timezone.utc), market="US")
    assert any(a.kind == "delete_membership" for a in actions)
    assert any(a.kind == "upsert_song" and a.row.get("liked") == 0 for a in actions)


@pytest.mark.parametrize("code", [429, 503])
def test_transport_failure_aborts_observation_instead_of_claiming_user_removal(code):
    request = httpx.Request("GET", "https://api.spotify.com/v1/tracks/old")
    response = httpx.Response(code, request=request, headers={"Retry-After": "120"})
    error = httpx.HTTPStatusError("unavailable", request=request, response=response)
    with pytest.raises(httpx.HTTPStatusError):
        mirror.pull_live(Spotify(error), "US", "owner", catalog(), full=True)


def test_unavailable_playlist_is_not_rewritten_by_inbox_materialization():
    m = catalog("inbox")
    live = mirror.pull_live(Spotify(False), "US", "owner", m, full=True)
    actions = reconcile.plan(m, live, datetime.now(timezone.utc), market="US")
    assert not [
        a
        for a in actions
        if a.kind in ("arrange_items", "add_item", "remove_item", "delete_membership")
    ]


def test_daily_availability_audit_reads_even_unchanged_smart_playlist():
    m = catalog("smart")
    spotify = Spotify(False)
    spotify.get_playlists = lambda: [
        {"id": "P", "name": "Playlist", "owner": {"id": "owner"}, "snapshot_id": "before"}
    ]
    live = mirror.pull_live(
        spotify,
        "US",
        "owner",
        m,
        full=True,
        smart_orders={"P": ["spotify:track:old"]},
        availability_audit=True,
    )
    assert "P" not in live.raw.get("reused", {})
    assert live.raw["availability_holds"] == [["P", ISRC]]


@pytest.mark.parametrize("bad_identity", [False, True])
def test_only_fresh_consistent_same_recording_alias_can_replace_unavailable_item(bad_identity):
    from core.model import Live, LivePlaylist
    from core.mirror import item_from_raw

    m = catalog()
    old = item_from_raw({"item": track(False), "added_at": STAMP})
    replacement = track(True, "new")
    if bad_identity:
        replacement["name"] = "Song (Live)"
    live = Live(
        {"P": LivePlaylist("P", "Playlist", None, "after", [old])},
        {ISRC: old},
        {"replacement_candidates": [{"item": replacement, "added_at": STAMP}]},
    )
    actions = reconcile.plan(m, live, datetime.now(timezone.utc), market="US")
    adds = [a for a in actions if a.kind == "add_item"]
    if bad_identity:
        assert adds == [] and any(a.kind == "flag" for a in actions)
    else:
        assert len(adds) == 1 and adds[0].uri == "spotify:track:new"
        members = [a for a in actions if a.kind == "upsert_membership"]
        assert members[-1].row["added_at"] == STAMP
        assert members[-1].row["spotify_track_id"] == "new"
        assert not [a for a in actions if a.kind == "upsert_song" and a.row.get("liked") == 0]


def test_availability_recovery_restores_preserved_intent_instead_of_calling_it_user_removal():
    m = catalog()
    live = mirror.pull_live(
        Spotify(True),
        "US",
        "owner",
        m,
        full=True,
        previous_holds=[["P", ISRC]],
        previous_likes=[ISRC],
    )
    actions = reconcile.plan(m, live, datetime.now(timezone.utc), market="US")
    assert not [a for a in actions if a.kind == "delete_membership"]
    assert any(a.kind == "add_item" and a.uri == "spotify:track:old" for a in actions)
    assert any(a.kind == "like" and a.uri == "spotify:track:old" for a in actions)


def test_spotify_404_is_retained_as_unavailable_without_erasing_metadata():
    request = httpx.Request("GET", "https://api.spotify.com/v1/tracks/old")
    response = httpx.Response(404, request=request)
    live = mirror.pull_live(
        Spotify(httpx.HTTPStatusError("missing", request=request, response=response)),
        "US",
        "owner",
        catalog(),
        full=True,
    )
    assert live.raw["availability_holds"] == [["P", ISRC]]
    actions = reconcile.plan(catalog(), live, datetime.now(timezone.utc), market="US")
    assert any(a.kind == "upsert_song" and a.row.get("spotify_playable") == 0 for a in actions)
    assert not any(a.kind == "delete_membership" for a in actions)


@pytest.mark.parametrize(
    "changes",
    [
        {"is_playable": None},
        {"external_ids": {"isrc": "USAAA2600002"}},
        {"duration_ms": None},
        {"duration_ms": 190000},
        {"artists": [{"name": "Other artist"}]},
    ],
)
def test_uncertain_alternative_does_not_authorize_automatic_replacement(changes):
    from core import metadata

    candidate = mirror.item_from_raw({"item": {**track(True, "new"), **changes}, "added_at": STAMP})
    assert metadata.replacement(catalog().songs[ISRC], [candidate]) is None


def test_alias_replacement_preserves_playlist_position_and_uses_actual_prewrite_order():
    from core.model import Live, LivePlaylist

    m = catalog()
    old = mirror.item_from_raw({"item": track(False), "added_at": STAMP})
    other = mirror.item_from_raw({"item": track(True, "other", "USAAA2600002"), "added_at": STAMP})
    candidate = {"item": track(True, "new"), "added_at": STAMP}
    live = Live(
        {"P": LivePlaylist("P", "Playlist", None, "after", [old, other])},
        {ISRC: old},
        {"replacement_candidates": [candidate]},
    )
    actions = reconcile.plan(m, live, datetime.now(timezone.utc), market="US")
    arrangements = [a for a in actions if a.kind == "arrange_items"]
    assert arrangements[-1].uris == ["spotify:track:new", "spotify:track:other"]
    assert arrangements[-1].current == ["spotify:track:old", "spotify:track:other"]


def test_another_held_item_prevents_partial_alias_catalog_update():
    from core.model import Live, LivePlaylist

    m = catalog()
    old = mirror.item_from_raw({"item": track(False), "added_at": STAMP})
    other = mirror.item_from_raw({"item": track(False, "other", "USAAA2600002"), "added_at": STAMP})
    live = Live(
        {"P": LivePlaylist("P", "Playlist", None, "after", [old, other])},
        {ISRC: old},
        {"replacement_candidates": [{"item": track(True, "new"), "added_at": STAMP}]},
    )
    actions = reconcile.plan(m, live, datetime.now(timezone.utc), market="US")
    assert not any(a.kind in ("add_item", "remove_item", "arrange_items") for a in actions)
    assert not any(
        a.kind == "upsert_membership" and a.isrc == ISRC and a.row["spotify_track_id"] == "new"
        for a in actions
    )


def test_retired_alias_does_not_prevent_unliking_a_playable_recording():
    m = catalog()
    m.songs[ISRC].spotify_ids = ["old", "new"]
    spotify = Spotify(True)
    spotify.get_track = lambda tid, market: track(tid == "new", tid)
    live = mirror.pull_live(spotify, "US", "owner", m, full=True)
    assert ISRC not in live.liked, "one fresh playable alias establishes an actual unlike"
