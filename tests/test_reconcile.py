from datetime import datetime, timedelta, timezone

import pytest

from core import reconcile
from core.model import Live, LiveItem, LivePlaylist, Membership, Mirror, Playlist, Song

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
T = "2026-09-01T00:00:00.000Z"


def song(isrc, liked=1, ids=None, playable=1, year=2010, genres=("Pop",)):
    ids = ids if ids is not None else ("t" + isrc,)
    return Song(
        isrc,
        liked,
        T if liked else None,
        T,
        list(ids),
        playable,
        year,
        list(genres),
        [],
        "n",
        ["a"],
    )


def item(isrc, track_id=None, added=T, playable=True):
    tid = track_id or ("t" + isrc if isrc else None)
    return LiveItem(
        isrc,
        tid,
        f"spotify:track:{tid}" if tid else "spotify:local:x",
        added,
        playable,
        isrc is None,
        "n",
        ["a"],
    )


def pl(pid, name, kind, rule=None, pinned=1, expires=None):
    return Playlist(pid, name, kind, rule, None, "snap", pinned, expires)


def live_pl(pid, name, items, desc=None):
    return LivePlaylist(pid, name, desc, "snap2", items)


def kinds(actions, kind):
    return [a for a in actions if a.kind == kind]


@pytest.mark.parametrize("known_second", [False, True])
def test_new_live_name_collision_cannot_change_smart_membership(known_second):
    playlists = {
        "P": pl("P", "Source", "curated"),
        "S": pl("S", "Smart", "smart", {"v": 1, "in_playlist_any": ["Source"]}),
    }
    if known_second:
        playlists["Q"] = pl("Q", "Other", "curated")
    m = Mirror({"A": song("A")}, playlists, {}, [], set())
    live = Live(
        {
            "P": live_pl("P", "Source", [item("A")]),
            "Q": live_pl("Q", "Source", []),
            "S": live_pl("S", "Smart", [item("A")]),
        },
        {"A": item("A")},
        {},
    )
    acts = reconcile.plan(m, live, NOW)
    assert not [
        a
        for a in acts
        if a.playlist_id == "S" and a.kind in {"remove_item", "add_item", "set_description"}
    ]
    assert any("ambiguous playlist" in a.text for a in kinds(acts, "flag"))


def test_new_song_keeps_observed_metadata_without_derivation():
    live = Live({}, {"A": item("A", track_id="observed")}, {})
    actions = reconcile.plan(Mirror({}, {}, {}, [], set()), live, NOW)
    row = next(a.row for a in actions if a.kind == "upsert_song")
    assert row["title"] == "n"
    assert row["artists"] == ["a"]
    assert row["spotify_ids"] == ["observed"]


@pytest.mark.parametrize("observation_only", [False, True])
def test_first_song_write_and_fifo_keep_metadata_in_both_paths(observation_only):
    m = Mirror({}, {"IN": pl("IN", "inbox", "inbox")}, {}, [], set())
    live = Live({"IN": live_pl("IN", "inbox", [item("A")])}, {}, {})
    acts = reconcile.plan(
        m,
        live,
        NOW,
        inbox_cap=0,
        observation_only=observation_only,
        source_ref="raw/spotify-pull/test.json.gz",
        market="US",
    )
    row = kinds(acts, "upsert_song")[0].row
    assert row["title"] == "n" and row["spotify_ids"] == ["tA"]
    assert row["liked"] == 0 and row["first_seen"] == "2026-09-08T12:00:00.000Z"
    assert bool(kinds(acts, "arrange_items")) is (not observation_only)
    assert any(a.row["rel"] == "evidence_of" for a in kinds(acts, "edge"))


def test_first_import_enters_smart_pool_using_observed_id():
    m = Mirror({}, {"SM": pl("SM", "pool", "smart", {"v": 1})}, {}, [], set())
    live = Live({"SM": live_pl("SM", "pool", [])}, {"A": item("A", "observed")}, {})
    acts = reconcile.plan(m, live, NOW)
    assert [(a.playlist_id, a.uri) for a in kinds(acts, "add_item")] == [
        ("SM", "spotify:track:observed")
    ]
    assert m.songs == {}


def test_existing_membership_id_is_usable_without_catalog_aliases():
    m = Mirror(
        {"A": song("A", ids=[])},
        {"SM": pl("SM", "pool", "smart", {"v": 1})},
        {
            ("OTHER", "A"): Membership("OTHER", "A", "z-member-id", T),
            ("CU", "A"): Membership("CU", "A", "member-id", T),
        },
        [],
        set(),
    )
    li = LiveItem("A", None, None, T, None, False, None, [])
    acts = reconcile.plan(m, Live({"SM": live_pl("SM", "pool", [])}, {"A": li}, {}), NOW)
    assert [a.uri for a in kinds(acts, "add_item")] == ["spotify:track:member-id"]


def test_relike_does_not_restore_retained_membership():
    m = Mirror(
        {"A": song("A", liked=0, ids=[])},
        {"CU": pl("CU", "curated", "curated")},
        {},
        [Membership("CU", "A", "undo-id", T, "2026-09-07T00:00:00.000Z")],
        set(),
    )
    li = LiveItem("A", None, None, T, None, False, None, [])
    acts = reconcile.plan(m, Live({"CU": live_pl("CU", "curated", [])}, {"A": li}, {}), NOW)
    assert not kinds(acts, "add_item")


@pytest.mark.parametrize("route", ["verified", "live", "membership"])
def test_routing_membership_inspections_are_bounded(route):
    class CountedMemberships(dict):
        inspections = 0

        def values(self):
            for membership in super().values():
                self.inspections += 1
                yield membership

    ids = [f"S{i:03}" for i in range(100)]
    members = CountedMemberships(
        {("OTHER", i): Membership("OTHER", i, f"member-{i}", T) for i in ids}
    )
    undo = route == "fallback"
    m = Mirror(
        {i: song(i, liked=0 if undo else 1, ids=[f"alias-{i}"]) for i in ids},
        {
            "TARGET": pl(
                "TARGET", "target", "curated" if undo else "smart", None if undo else {"v": 1}
            )
        },
        members,
        [Membership("TARGET", i, f"undo-{i}", T, "2026-09-07T00:00:00.000Z") for i in ids]
        if undo
        else [],
        set(),
    )
    liked = {
        i: item(i, f"live-{i}", playable=None)
        if route in ("verified", "live")
        else LiveItem(i, None, None, T, None, False, None, [])
        for i in ids
    }
    observations = [item(i, f"verified-{i}") for i in ids] if route == "verified" else []
    acts = reconcile.plan(
        m, Live({"TARGET": live_pl("TARGET", "target", [])}, liked, {}, observations), NOW
    )
    prefix = {"verified": "verified", "live": "live", "fallback": "undo", "membership": "member"}[
        route
    ]
    assert [a.uri for a in kinds(acts, "add_item")] == [f"spotify:track:{prefix}-{i}" for i in ids]
    assert members.inspections <= (100 if route == "membership" else 0)


@pytest.mark.parametrize("availability", [None, False])
def test_unknown_and_unverified_alias_never_trigger_relink(availability):
    m = Mirror(
        {"A": song("A", ids=["unverified", "actual"])},
        {"CU": pl("CU", "curated", "curated")},
        {},
        [],
        set(),
    )
    it = item("A", "actual", playable=availability)
    acts = reconcile.plan(m, Live({"CU": live_pl("CU", "curated", [it])}, {"A": it}, {}), NOW)
    assert not kinds(acts, "add_item") and not kinds(acts, "remove_item")
    assert bool(kinds(acts, "flag")) is (availability is False)


@pytest.mark.parametrize("market, expected", [("US", []), ("GB", [])])
def test_positive_evidence_does_not_authorize_alias_replacement(market, expected):
    m = Mirror(
        {"A": song("A", ids=["legacy", "actual", "verified"])},
        {"CU": pl("CU", "curated", "curated")},
        {},
        [],
        set(),
        observations=[
            {
                "id": "observation",
                "to_ref": "A",
                "detail": {
                    "field": "spotify_playable",
                    "track_id": "verified",
                    "value": True,
                    "market": "US",
                },
            }
        ],
    )
    it = item("A", "actual", playable=False)
    acts = reconcile.plan(
        m, Live({"CU": live_pl("CU", "curated", [it])}, {"A": it}, {}), NOW, market=market
    )
    assert [a.uri for a in kinds(acts, "add_item")] == expected


def test_display_representative_does_not_choose_unavailable_routing_target():
    from core.mirror import load_mirror
    from tests.test_metadata import observe, live as metadata_live, raw, persisted, ISRC
    from tests.test_mirror import FakeHub

    m = load_mirror(FakeHub(persisted(observe(state=metadata_live(raw("display"))))))
    m.playlists["SM"] = pl("SM", "pool", "smart", {"v": 1})
    display = item(ISRC, "display", playable=False)
    good = item(ISRC, "playable", playable=True)
    state = Live({"SM": live_pl("SM", "pool", [])}, {ISRC: display}, {}, [display, good])
    acts = reconcile.plan(m, state, NOW, market="US", source_ref="raw/spotify-pull/next.json.gz")
    assert [a.uri for a in kinds(acts, "add_item")] == ["spotify:track:playable"]
    title = next(a.row for a in kinds(acts, "edge") if a.row["detail"].get("field") == "title")
    assert title["detail"]["track_id"] == "display"


def test_smart_pool_does_not_readd_newly_observed_unhearted_song():
    m = Mirror({"A": song("A")}, {"SM": pl("SM", "pool", "smart", {"v": 1})}, {}, [], set())
    acts = reconcile.plan(
        m, Live({"SM": live_pl("SM", "pool", [item("A", "new-alias")])}, {}, {}), NOW
    )
    assert not kinds(acts, "add_item") and not kinds(acts, "like")
    assert [a.uri for a in kinds(acts, "remove_item")] == ["spotify:track:new-alias"]
    assert any(a.row.get("spotify_ids") == ["tA", "new-alias"] for a in kinds(acts, "upsert_song"))


def base():
    songs = {"A": song("A"), "B": song("B"), "C": song("C", liked=0)}
    playlists = {
        "IN": pl("IN", "new songs", "inbox"),
        "CU": pl("CU", "mood list", "curated"),
        "SM": pl("SM", "pop", "smart", {"v": 1, "deezer_genres_any": ["Pop"]}),
    }
    memberships = {
        ("CU", "A"): Membership("CU", "A", "tA", T),
        ("SM", "A"): Membership("SM", "A", "tA", T),
        ("SM", "B"): Membership("SM", "B", "tB", T),
    }
    return Mirror(songs, playlists, memberships, [], set())


def test_added_to_curated_while_unliked_gets_liked_and_edge():
    m = base()
    live = Live(
        {
            "CU": live_pl("CU", "mood list", [item("A"), item("C")]),
            "SM": live_pl("SM", "pop", [item("A"), item("B")]),
            "IN": live_pl("IN", "new songs", []),
        },
        {"A": item("A"), "B": item("B")},
        {},
    )
    acts = reconcile.plan(m, live, NOW, curation_likes={"C"})
    assert [a.uri for a in kinds(acts, "like")] == ["spotify:track:tC"]
    assert any(a.kind == "upsert_song" and a.row["id"] == "C" and a.row["liked"] == 1 for a in acts)
    assert any(
        a.kind == "edge"
        and a.row["to_ref"] == "C"
        and a.row["from_kind"] == "playlist"
        and a.row["from_ref"] == "CU"
        for a in acts
    )


def test_unheart_preserves_curated_and_smart_rule_removes():
    m = base()
    live = Live(
        {
            "CU": live_pl("CU", "mood list", [item("A")]),
            "SM": live_pl("SM", "pop", [item("A"), item("B")]),
            "IN": live_pl("IN", "new songs", [item("A")]),
        },
        {"B": item("B")},
        {},
    )
    acts = reconcile.plan(m, live, NOW)
    removed = {(a.playlist_id, a.uri) for a in kinds(acts, "remove_item")}
    assert removed == {("SM", "spotify:track:tA")}
    assert not kinds(acts, "like")
    assert {a.row["id"] for a in kinds(acts, "delete_membership")} == {"SM:A"}


def test_unheart_does_not_mutate_skipped_playlists():
    # A missing playlist pull cannot authorize removals.
    m = base()
    live = Live(
        {
            "CU": live_pl("CU", "mood list", None),
            "SM": live_pl("SM", "pop", None),
            "IN": live_pl("IN", "new songs", None),
        },
        {"B": item("B")},
        {},
    )
    acts = reconcile.plan(m, live, NOW)
    removed = {(a.playlist_id, a.uri) for a in kinds(acts, "remove_item")}
    assert removed == set()
    assert {a.row["id"] for a in kinds(acts, "delete_membership")} == set()


def test_smart_materializes_when_curated_and_inbox_skipped():
    m = base()
    m.songs["A"].liked = 0
    m.songs["A"].liked_at = None
    m.memberships.pop(("SM", "A"))
    live = Live(
        {
            "CU": live_pl("CU", "mood list", None),
            "SM": live_pl("SM", "pop", [item("B")]),
            "IN": live_pl("IN", "new songs", None),
        },
        {"A": item("A"), "B": item("B")},
        {},
    )
    acts = reconcile.plan(m, live, NOW)
    assert ("SM", "spotify:track:tA") in {(a.playlist_id, a.uri) for a in kinds(acts, "add_item")}


def test_rule_error_flags_instead_of_raising_other_smart_playlists_still_materialize():
    m = base()
    m.songs["D"] = song("D", genres=["Pop"])
    m.playlists["BAD"] = pl("BAD", "bad rule", "smart", {"v": 1, "in_playlist_any": ["gone"]})
    live = Live(
        {
            "CU": live_pl("CU", "mood list", [item("A")]),
            "SM": live_pl("SM", "pop", [item("A"), item("B")]),
            "IN": live_pl("IN", "new songs", []),
        },
        {"A": item("A"), "B": item("B"), "D": item("D")},
        {},
    )
    acts = reconcile.plan(m, live, NOW)
    rule_flags = [a for a in kinds(acts, "flag") if a.reason == "rule"]
    assert len(rule_flags) == 1
    assert "bad rule" in rule_flags[0].text and "gone" in rule_flags[0].text
    assert [a.uri for a in kinds(acts, "add_item") if a.playlist_id == "SM"] == ["spotify:track:tD"]


def test_relike_never_restores_curated_regardless_tombstone_age():
    m = base()
    m.songs["A"].liked = 0
    m.memberships.pop(("CU", "A"))
    m.deleted_memberships.append(
        Membership("CU", "A", "tA", T, deleted_at=reconcile._iso(NOW - timedelta(days=2)))
    )
    live = Live(
        {
            "CU": live_pl("CU", "mood list", []),
            "SM": live_pl("SM", "pop", [item("B")]),
            "IN": live_pl("IN", "new songs", []),
        },
        {"A": item("A"), "B": item("B")},
        {},
    )
    acts = reconcile.plan(m, live, NOW)
    assert ("CU", "spotify:track:tA") not in {
        (a.playlist_id, a.uri) for a in kinds(acts, "add_item")
    }
    # plan() must not mutate its input: the mirror's own "A" is still unliked, so the
    # second call below is a genuine test of the cutoff window, not of a stale mutation
    assert m.songs["A"].liked == 0
    m.deleted_memberships[0].deleted_at = reconcile._iso(NOW - timedelta(days=9))
    assert ("CU", "spotify:track:tA") not in {
        (a.playlist_id, a.uri) for a in kinds(reconcile.plan(m, live, NOW), "add_item")
    }


def test_new_songs_cap_and_exemption():
    m = base()
    items = [item(f"Z{i:02d}", added=f"2026-08-{i + 1:02d}T00:00:00.000Z") for i in range(3)]
    live = Live(
        {
            "IN": live_pl("IN", "new songs", items),
            "CU": live_pl("CU", "mood list", [item("A")]),
            "SM": live_pl("SM", "pop", [item("A"), item("B")]),
        },
        {"A": item("A"), "B": item("B")},
        {},
    )
    acts = reconcile.plan(m, live, NOW, inbox_cap=3)
    # Songs new to the catalog you put in New songs count from their add time; B's
    # like (2026-09-01) is newer, so it leads and the oldest hand-add falls off. A was
    # already curated when liked, so that like is Music Sync's, not A coming your way.
    inbox = [a for a in kinds(acts, "arrange_items") if a.playlist_id == "IN"]
    assert [a.uri for a in inbox] == [None]
    assert inbox[0].uris == [
        "spotify:track:tB",
        "spotify:track:tZ02",
        "spotify:track:tZ01",
    ]
    found = {a.isrc: a.row.get("found_at") for a in kinds(acts, "upsert_song")}
    assert found["Z00"] == "2026-08-01T00:00:00.000Z"
    assert not [a for a in kinds(acts, "like")]  # unliked inbox songs are never liked


def test_smart_materialization_and_description():
    m = base()
    m.songs["D"] = song("D", genres=["Pop"])
    m.songs["B"].deezer_genres = ["Rock"]
    live = Live(
        {
            "SM": live_pl(
                "SM", "pop", [item("A"), item("B")], desc="smart · genre: Pop · synced 2026-09-01"
            ),
            "CU": live_pl("CU", "mood list", [item("A")]),
            "IN": live_pl("IN", "new songs", []),
        },
        {"A": item("A"), "B": item("B"), "D": item("D")},
        {},
    )
    acts = reconcile.plan(m, live, NOW, today="2026-09-08")
    assert [a.uri for a in kinds(acts, "add_item") if a.playlist_id == "SM"] == ["spotify:track:tD"]
    assert [a.uri for a in kinds(acts, "remove_item") if a.playlist_id == "SM"] == [
        "spotify:track:tB"
    ]
    assert not kinds(acts, "set_description")  # rule unchanged: no daily rewrite
    m.playlists["SM"].rule = {"v": 1, "deezer_genres_any": ["Pop"], "first_year": {"lt": 2020}}
    acts = reconcile.plan(m, live, NOW, today="2026-09-08")
    assert (
        kinds(acts, "set_description")[0].text
        == "smart · genre: Pop · year < 2020 · synced 2026-09-08"
    )


def test_unplayable_requires_review():
    m = base()
    m.songs["A"].spotify_ids = ["tA2", "tA"]
    m.songs["B"].spotify_playable = 0
    live = Live(
        {
            "CU": live_pl(
                "CU", "mood list", [item("A", playable=False), item("B", playable=False)]
            ),
            "SM": live_pl("SM", "pop", []),
            "IN": live_pl("IN", "new songs", []),
        },
        {"A": item("A", "tA2"), "B": item("B", playable=False)},
        {},
    )
    acts = reconcile.plan(m, live, NOW)
    assert not [a for a in kinds(acts, "remove_item") if a.playlist_id == "CU"]
    assert not [a for a in kinds(acts, "add_item") if a.playlist_id == "CU"]
    assert any(a.kind == "flag" and "B" in a.text for a in acts)


def test_duplicate_isrc_requires_keeper_review():
    m = base()
    live = Live(
        {
            "CU": live_pl(
                "CU",
                "mood list",
                [
                    item("A", "tA", added="2026-08-02T00:00:00.000Z"),
                    item("A", "tA9", added="2026-08-01T00:00:00.000Z"),
                ],
            ),
            "SM": live_pl("SM", "pop", []),
            "IN": live_pl("IN", "new songs", []),
        },
        {"A": item("A")},
        {},
    )
    acts = reconcile.plan(m, live, NOW)
    assert not [a for a in kinds(acts, "remove_item") if a.playlist_id == "CU"]
    assert any("keeper" in a.text for a in kinds(acts, "flag"))
    assert not kinds(acts, "readd_item")  # different uris: no readd needed


def test_duplicate_same_uri_is_preserved_for_review():
    m = base()
    live = Live(
        {
            "CU": live_pl(
                "CU",
                "mood list",
                [
                    item("A", "tA", added="2026-08-02T00:00:00.000Z"),
                    item("A", "tA", added="2026-08-01T00:00:00.000Z"),
                ],
            ),
            "SM": live_pl("SM", "pop", []),
            "IN": live_pl("IN", "new songs", []),
        },
        {"A": item("A")},
        {},
    )
    acts = reconcile.plan(m, live, NOW)
    assert not [a for a in kinds(acts, "remove_item") if a.playlist_id == "CU"]
    assert any("keeper" in a.text for a in kinds(acts, "flag"))
    assert not kinds(acts, "readd_item")


def test_ephemeral_expiry_and_skipped_playlists():
    m = base()
    m.playlists["SM"].pinned, m.playlists["SM"].expires_at = (
        0,
        reconcile._iso(NOW - timedelta(hours=1)),
    )
    live = Live(
        {
            "SM": live_pl("SM", "pop", None),
            "CU": live_pl("CU", "mood list", None),
            "IN": live_pl("IN", "new songs", None),
        },
        {"A": item("A"), "B": item("B")},
        {},
    )
    acts = reconcile.plan(m, live, NOW)
    assert [a.playlist_id for a in kinds(acts, "delete_playlist")] == ["SM"]
    assert not kinds(acts, "remove_item") and not kinds(acts, "delete_membership")
    # soft-delete must not be overwritten by mirror upkeep's plain upsert_playlist
    sm_upserts = [a for a in kinds(acts, "upsert_playlist") if a.playlist_id == "SM"]
    assert len(sm_upserts) == 1 and sm_upserts[0].row["deleted_at"] == reconcile._iso(NOW)


def test_no_isrc_items_are_one_flag_not_rows():
    m = base()
    live = Live(
        {
            "CU": live_pl("CU", "mood list", [item(None)]),
            "SM": live_pl("SM", "pop", []),
            "IN": live_pl("IN", "new songs", []),
        },
        {"A": item("A"), "B": item("B")},
        {},
    )
    acts = reconcile.plan(m, live, NOW)
    flags = [a for a in kinds(acts, "flag") if a.reason == "no_isrc"]
    assert len(flags) == 1 and not any(
        a.kind == "upsert_song" and a.row["id"] is None for a in acts
    )


def test_inbox_duplicates_are_reviewed_without_relink_or_dedupe():
    m = base()
    m.songs["A"].spotify_ids = ["tA2", "tA"]
    items = [
        item("A", "tA", added=T, playable=False),
        item("A", "tA9", added="2026-08-31T00:00:00.000Z"),
    ]
    live = Live(
        {
            "IN": live_pl("IN", "new songs", items),
            "CU": live_pl("CU", "mood list", []),
            "SM": live_pl("SM", "pop", []),
        },
        {"A": item("A")},
        {},
    )
    acts = reconcile.plan(m, live, NOW)
    assert not [a for a in kinds(acts, "remove_item") if a.playlist_id == "IN"]
    assert not [a for a in kinds(acts, "add_item") if a.playlist_id == "IN"]
    assert any("keeper" in a.text for a in kinds(acts, "flag") if a.reason == "attention")


def test_new_unliked_curated_song_has_one_edge_created_row_1():
    m = base()
    live = Live(
        {
            "CU": live_pl("CU", "mood list", [item("A"), item("E")]),
            "SM": live_pl("SM", "pop", [item("A"), item("B")]),
            "IN": live_pl("IN", "new songs", []),
        },
        {"A": item("A"), "B": item("B")},
        {},
    )
    acts = reconcile.plan(m, live, NOW, curation_likes={"E"})
    edges = [a for a in kinds(acts, "edge") if a.row["to_ref"] == "E"]
    assert len(edges) == 1 and edges[0].row["detail"]["created_row"] == 1
    assert [a.uri for a in kinds(acts, "like") if a.isrc == "E"] == ["spotify:track:tE"]


def test_apply_order():
    m = base()
    m.songs["B"].deezer_genres = ["Rock"]
    live = Live(
        {
            "CU": live_pl("CU", "mood list", [item("A"), item("C")]),
            "SM": live_pl("SM", "pop", [item("A"), item("B")]),
            "IN": live_pl("IN", "new songs", []),
        },
        {"A": item("A"), "B": item("B")},
        {},
    )
    order = [a.kind for a in reconcile.plan(m, live, NOW)]
    rank = {k: i for i, k in enumerate(reconcile.ORDER)}
    assert order == sorted(order, key=lambda k: rank[k])


def test_replacement_alias_keeps_the_earliest_membership_date():
    m = Mirror(
        {"A": song("A")},
        {"CU": pl("CU", "curated", "curated")},
        {("CU", "A"): Membership("CU", "A", "old-alias", "2018-06-12T02:13:23.000Z")},
        [],
        set(),
    )
    replaced = item("A", track_id="new-alias", added="2026-10-09T12:00:00Z")
    acts = reconcile.plan(
        m, Live({"CU": live_pl("CU", "curated", [replaced])}, {"A": replaced}, {}), NOW
    )
    row = next(a.row for a in kinds(acts, "upsert_membership") if a.playlist_id == "CU")
    assert row["spotify_track_id"] == "new-alias"
    assert row["added_at"] == "2018-06-12T02:13:23.000Z"


def test_readded_smart_member_is_not_tombstoned_in_the_same_run():
    m = Mirror(
        {"A": song("A")},
        {"SM": pl("SM", "pool", "smart", {"v": 1})},
        {("SM", "A"): Membership("SM", "A", "tA", "2020-01-01T00:00:00.000Z")},
        [],
        set(),
    )
    live = Live({"SM": live_pl("SM", "pool", [])}, {"A": item("A")}, {})
    acts = reconcile.plan(m, live, NOW)
    assert [a.isrc for a in kinds(acts, "add_item")] == ["A"]
    assert not [a for a in kinds(acts, "delete_membership") if a.playlist_id == "SM"]
    row = next(a.row for a in kinds(acts, "upsert_membership") if a.playlist_id == "SM")
    assert row["deleted_at"] is None and row["added_at"] == "2020-01-01T00:00:00.000Z"


def test_missing_or_duplicate_inbox_is_flagged():
    m = Mirror(
        {},
        {"N": pl("N", "new songs", "inbox"), "M": pl("M", "other inbox", "inbox")},
        {},
        [],
        set(),
    )
    acts = reconcile.plan(m, Live({"M": live_pl("M", "other inbox", [])}, {}, {}), NOW)
    texts = [a.text for a in kinds(acts, "flag")]
    assert any("N: inbox playlist missing from Spotify" in t for t in texts)
    assert any("2 inbox playlists" in t for t in texts)


def test_observation_import_patches_only_recordings_it_catalogs():
    m = Mirror({}, {"CU": pl("CU", "curated list", "curated")}, {}, [], set())
    known = item("A")
    unclassified = LiveItem("B", "tB", "spotify:track:tB", T, True, False, "Only there", ["x"])
    live = Live(
        {
            "CU": live_pl("CU", "curated list", [known]),
            "NEW": live_pl("NEW", "unclassified", [unclassified]),
        },
        {},
        {},
    )
    acts = reconcile.plan(m, live, NOW, observation_only=True, source_ref="raw/x", market="US")
    assert {a.isrc for a in acts if a.kind in ("upsert_song", "edge")} == {"A"}
    assert any("classification" in (a.text or "") for a in kinds(acts, "flag"))


# New songs: the inbox_cap songs that most recently came your way, newest first.
T0, T1, T2, T3, T4 = (f"2026-09-0{d}T00:00:00.000Z" for d in range(1, 6))


def timed(isrc, liked=0, liked_at=None, **kw):
    s = song(isrc, liked=liked)
    s.liked_at = liked_at
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def inbox_mirror(songs, members=(), deleted=()):
    memberships = {("IN", i): Membership("IN", i, "t" + i, T0) for i in members}
    gone = [Membership("IN", i, "t" + i, T0, at) for i, at in deleted]
    return Mirror(
        {s.id: s for s in songs}, {"IN": pl("IN", "new songs", "inbox")}, memberships, gone, set()
    )


def arranged(acts):
    out = kinds(acts, "arrange_items")
    assert len(out) <= 1
    return out[0].uris if out else None


def test_new_songs_holds_the_latest_songs_newest_first_with_likes_and_shazams_interleaved():
    m = inbox_mirror(
        [
            timed("OLD", liked=1, liked_at=T0),
            timed("FOUND", found_at=T1),
            timed("LIKED", liked=1, liked_at=T2),
            timed("SHAZ", shazam_last_at=T3),
        ],
        members=["OLD", "FOUND"],
    )
    live = Live(
        {"IN": live_pl("IN", "new songs", [item("OLD"), item("FOUND")])},
        {"OLD": item("OLD", added=T0), "LIKED": item("LIKED", added=T2)},
        {},
    )
    acts = reconcile.plan(m, live, NOW, inbox_cap=3)
    assert arranged(acts) == ["spotify:track:tSHAZ", "spotify:track:tLIKED", "spotify:track:tFOUND"]
    assert {a.isrc for a in kinds(acts, "upsert_membership") if a.playlist_id == "IN"} == {
        "SHAZ",
        "LIKED",
    }
    assert [a.isrc for a in kinds(acts, "delete_membership") if a.playlist_id == "IN"] == ["OLD"]
    assert not [a for a in acts if a.playlist_id == "IN" and a.kind in ("add_item", "remove_item")]


def test_no_arrangement_when_new_songs_already_holds_the_order():
    m = inbox_mirror(
        [timed("A", shazam_last_at=T2), timed("B", shazam_last_at=T1)], members=["A", "B"]
    )
    live = Live({"IN": live_pl("IN", "new songs", [item("A"), item("B")])}, {}, {})
    assert arranged(reconcile.plan(m, live, NOW)) is None


def test_a_song_new_to_the_catalog_added_to_a_curated_playlist_is_found_at_its_add_time():
    m = inbox_mirror([timed("A", shazam_last_at=T1)], members=["A"])
    m.playlists["CU"] = pl("CU", "mood", "curated")
    live = Live(
        {
            "IN": live_pl("IN", "new songs", [item("A")]),
            "CU": live_pl("CU", "mood", [item("NEW", added=T4)]),
        },
        {},
        {},
    )
    acts = reconcile.plan(m, live, NOW)
    row = next(a.row for a in kinds(acts, "upsert_song") if a.isrc == "NEW")
    assert row["found_at"] == T4
    assert arranged(acts) == ["spotify:track:tNEW", "spotify:track:tA"]


def test_a_hand_add_to_new_songs_moves_a_known_song_to_its_add_time():
    m = inbox_mirror([timed("A", shazam_last_at=T2), timed("K", shazam_last_at=T0)], members=["A"])
    live = Live({"IN": live_pl("IN", "new songs", [item("A"), item("K", added=T4)])}, {}, {})
    acts = reconcile.plan(m, live, NOW)
    assert any(a.isrc == "K" and a.row.get("found_at") == T4 for a in kinds(acts, "upsert_song"))
    assert arranged(acts) == ["spotify:track:tK", "spotify:track:tA"]


def test_unliking_drops_a_song_unless_a_shazam_keeps_it():
    m = inbox_mirror(
        [
            timed("ONLY", liked=1, liked_at=T3),
            timed("BOTH", liked=1, liked_at=T3, shazam_last_at=T1),
        ],
        members=["ONLY", "BOTH"],
    )
    live = Live({"IN": live_pl("IN", "new songs", [item("ONLY"), item("BOTH")])}, {}, {})
    assert arranged(reconcile.plan(m, live, NOW)) == ["spotify:track:tBOTH"]


def test_a_song_removed_from_new_songs_by_hand_stays_out_until_it_comes_back():
    m = inbox_mirror(
        [timed("A", shazam_last_at=T2), timed("GONE", shazam_last_at=T1)],
        members=["A"],
        deleted=[("GONE", T3)],
    )
    live = Live({"IN": live_pl("IN", "new songs", [item("A")])}, {}, {})
    assert arranged(reconcile.plan(m, live, NOW)) is None
    m.songs["GONE"].shazam_last_at = T4  # Shazamed again after it was removed
    assert arranged(reconcile.plan(m, live, NOW)) == ["spotify:track:tGONE", "spotify:track:tA"]


def test_new_songs_keeps_an_alias_it_already_holds():
    m = inbox_mirror(
        [timed("A", shazam_last_at=T2), timed("B", shazam_last_at=T3)], members=["A", "B"]
    )
    live = Live(
        {"IN": live_pl("IN", "new songs", [item("A", track_id="alias"), item("B")])}, {}, {}
    )
    assert arranged(reconcile.plan(m, live, NOW)) == ["spotify:track:tB", "spotify:track:alias"]


def test_an_item_without_an_isrc_leaves_new_songs_unarranged_and_flags():
    m = inbox_mirror([timed("A", shazam_last_at=T2), timed("B", shazam_last_at=T3)], members=["A"])
    live = Live({"IN": live_pl("IN", "new songs", [item("A"), item(None)])}, {}, {})
    acts = reconcile.plan(m, live, NOW)
    assert arranged(acts) is None
    assert any("order is left alone" in a.text for a in kinds(acts, "flag"))


def test_music_sync_liking_a_curated_song_does_not_bring_it_into_new_songs():
    m = inbox_mirror(
        [timed("CUR", liked=1, liked_at=T4), timed("A", shazam_last_at=T2)], members=["A"]
    )
    m.playlists["CU"] = pl("CU", "mood", "curated")
    m.memberships[("CU", "CUR")] = Membership("CU", "CUR", "tCUR", T1)  # curated before the like
    live = Live(
        {
            "IN": live_pl("IN", "new songs", [item("A")]),
            "CU": live_pl("CU", "mood", [item("CUR", added=T1)]),
        },
        {"CUR": item("CUR", added=T4)},
        {},
    )
    assert arranged(reconcile.plan(m, live, NOW)) is None


def smart_mirror(songs, members=()):
    memberships = {("SM", i): Membership("SM", i, "t" + i, T0) for i in members}
    return Mirror(
        {s.id: s for s in songs},
        {"SM": pl("SM", "pool", "smart", {"v": 1})},
        memberships,
        [],
        set(),
    )


def smart_arrangement(acts):
    out = [a for a in kinds(acts, "arrange_items") if a.playlist_id == "SM"]
    assert len(out) <= 1
    return out[0] if out else None


def test_a_smart_playlist_is_arranged_newest_first_by_when_songs_came_your_way():
    songs = [
        timed("OLD", liked=1, liked_at=T0),
        timed("SHAZ", liked=1, liked_at=T1, shazam_last_at=T3),
        timed("NEW", liked=1, liked_at=T2),
    ]
    m = smart_mirror(songs, members=["OLD", "SHAZ"])
    live = Live(
        {"SM": live_pl("SM", "pool", [item("OLD"), item("SHAZ")])},
        {s.id: item(s.id, added=s.liked_at) for s in songs},
        {},
    )
    acts = reconcile.plan(m, live, NOW)
    # Membership is still the rule's: one add, recorded as before.
    assert [a.isrc for a in kinds(acts, "add_item")] == ["NEW"]
    assert {a.isrc for a in kinds(acts, "upsert_membership") if a.playlist_id == "SM"} == {"NEW"}
    arrangement = smart_arrangement(acts)
    assert arrangement.uris == ["spotify:track:tSHAZ", "spotify:track:tNEW", "spotify:track:tOLD"]
    assert arrangement.current == ["spotify:track:tOLD", "spotify:track:tSHAZ"]


def test_no_smart_arrangement_when_the_playlist_already_holds_the_order():
    songs = [timed("A", liked=1, liked_at=T2), timed("B", liked=1, liked_at=T1)]
    m = smart_mirror(songs, members=["A", "B"])
    live = Live(
        {"SM": live_pl("SM", "pool", [item("A"), item("B")])},
        {s.id: item(s.id, added=s.liked_at) for s in songs},
        {},
    )
    assert smart_arrangement(reconcile.plan(m, live, NOW)) is None


def test_a_removed_smart_member_leaves_the_rest_in_order_without_an_arrangement():
    songs = [timed("A", liked=1, liked_at=T2), timed("B", liked=1, liked_at=T1)]
    m = smart_mirror(songs, members=["A", "B"])
    live = Live(
        {"SM": live_pl("SM", "pool", [item("A"), item("B")])},
        {"B": item("B", added=T1)},  # A was unliked
        {},
    )
    acts = reconcile.plan(m, live, NOW)
    assert [a.isrc for a in kinds(acts, "remove_item")] == ["A"]
    assert smart_arrangement(acts) is None  # a removal keeps the others' order


def test_an_item_without_an_isrc_leaves_a_smart_playlist_unarranged_but_materialized():
    songs = [timed("A", liked=1, liked_at=T1), timed("B", liked=1, liked_at=T2)]
    m = smart_mirror(songs, members=["A"])
    live = Live(
        {"SM": live_pl("SM", "pool", [item("A"), item(None)])},
        {s.id: item(s.id, added=s.liked_at) for s in songs},
        {},
    )
    acts = reconcile.plan(m, live, NOW)
    assert smart_arrangement(acts) is None
    assert [a.isrc for a in kinds(acts, "add_item")] == ["B"]
    assert any("pool: holds an item without an ISRC" in a.text for a in kinds(acts, "flag"))


def test_smart_orders_record_what_each_smart_playlist_holds_after_the_plan():
    m = smart_mirror([timed(i, liked=1, liked_at=T1) for i in "ABC"], members="ABC")
    m.playlists["CU"] = pl("CU", "mood", "curated")
    m.playlists["S2"] = pl("S2", "two", "smart", {"v": 1})
    m.playlists["S3"] = pl("S3", "three", "smart", {"v": 1})
    live = Live(
        {
            "SM": live_pl("SM", "pool", [item("A"), item("B"), item("C")]),
            "S2": live_pl("S2", "two", [item("A"), item("B")]),
            "S3": live_pl("S3", "three", [item("A"), item("A")]),
            "CU": live_pl("CU", "mood", [item("A")]),
        },
        {},
        {},
    )
    uri = "spotify:track:t"
    acts = [
        reconcile.Action("remove_item", playlist_id="SM", uri=uri + "B"),
        reconcile.Action("add_item", playlist_id="S2", uri=uri + "C"),
    ]
    assert reconcile.smart_orders(m, live, acts) == {"SM": [uri + "A", uri + "C"]}
    acts.append(reconcile.Action("arrange_items", playlist_id="S2", uris=[uri + "C", uri + "A"]))
    assert reconcile.smart_orders(m, live, acts)["S2"] == [uri + "C", uri + "A"]
