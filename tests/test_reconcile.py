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
    assert bool(kinds(acts, "remove_item")) is (not observation_only)
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


def test_undo_uses_retained_membership_id_without_alias_search():
    m = Mirror(
        {"A": song("A", liked=0, ids=[])},
        {"CU": pl("CU", "curated", "curated")},
        {},
        [Membership("CU", "A", "undo-id", T, "2026-09-07T00:00:00.000Z")],
        set(),
    )
    li = LiveItem("A", None, None, T, None, False, None, [])
    acts = reconcile.plan(m, Live({"CU": live_pl("CU", "curated", [])}, {"A": li}, {}), NOW)
    assert [a.uri for a in kinds(acts, "add_item")] == ["spotify:track:undo-id"]


@pytest.mark.parametrize("route", ["verified", "live", "fallback", "membership"])
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


@pytest.mark.parametrize("market, expected", [("US", ["spotify:track:verified"]), ("GB", [])])
def test_relink_uses_retained_positive_evidence_only_in_observed_market(market, expected):
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
        "CU": pl("CU", "feel good", "curated"),
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
            "CU": live_pl("CU", "feel good", [item("A"), item("C")]),
            "SM": live_pl("SM", "pop", [item("A"), item("B")]),
            "IN": live_pl("IN", "new songs", []),
        },
        {"A": item("A"), "B": item("B")},
        {},
    )
    acts = reconcile.plan(m, live, NOW)
    assert [a.uri for a in kinds(acts, "like")] == ["spotify:track:tC"]
    assert any(a.kind == "upsert_song" and a.row["id"] == "C" and a.row["liked"] == 1 for a in acts)
    assert any(
        a.kind == "edge"
        and a.row["to_ref"] == "C"
        and a.row["from_kind"] == "playlist"
        and a.row["from_ref"] == "CU"
        for a in acts
    )


def test_unheart_removes_from_curated_and_smart_and_wins_tie():
    m = base()
    live = Live(
        {
            "CU": live_pl("CU", "feel good", [item("A")]),
            "SM": live_pl("SM", "pop", [item("A"), item("B")]),
            "IN": live_pl("IN", "new songs", [item("A")]),
        },
        {"B": item("B")},
        {},
    )
    acts = reconcile.plan(m, live, NOW)
    removed = {(a.playlist_id, a.uri) for a in kinds(acts, "remove_item")}
    assert removed == {("CU", "spotify:track:tA"), ("SM", "spotify:track:tA")}
    assert not kinds(acts, "like")
    assert {a.row["id"] for a in kinds(acts, "delete_membership")} == {"CU:A", "SM:A"}


def test_unheart_removes_skipped_playlist_memberships_via_mirror():
    # steady state: hearting/un-hearting never bumps a curated playlist's snapshot, so
    # pull_live skips it (items=None) - un-heart must still fall back to the mirror row
    m = base()
    live = Live(
        {
            "CU": live_pl("CU", "feel good", None),
            "SM": live_pl("SM", "pop", None),
            "IN": live_pl("IN", "new songs", None),
        },
        {"B": item("B")},
        {},
    )
    acts = reconcile.plan(m, live, NOW)
    removed = {(a.playlist_id, a.uri) for a in kinds(acts, "remove_item")}
    assert removed == {("CU", "spotify:track:tA"), ("SM", "spotify:track:tA")}
    assert {a.row["id"] for a in kinds(acts, "delete_membership")} == {"CU:A", "SM:A"}


def test_smart_materializes_when_curated_and_inbox_skipped():
    m = base()
    m.songs["A"].liked = 0
    m.songs["A"].liked_at = None
    m.memberships.pop(("SM", "A"))
    live = Live(
        {
            "CU": live_pl("CU", "feel good", None),
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
            "CU": live_pl("CU", "feel good", [item("A")]),
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


def test_undo_restores_curated_within_window():
    m = base()
    m.songs["A"].liked = 0
    m.memberships.pop(("CU", "A"))
    m.deleted_memberships.append(
        Membership("CU", "A", "tA", T, deleted_at=reconcile._iso(NOW - timedelta(days=2)))
    )
    live = Live(
        {
            "CU": live_pl("CU", "feel good", []),
            "SM": live_pl("SM", "pop", [item("B")]),
            "IN": live_pl("IN", "new songs", []),
        },
        {"A": item("A"), "B": item("B")},
        {},
    )
    acts = reconcile.plan(m, live, NOW)
    assert ("CU", "spotify:track:tA") in {(a.playlist_id, a.uri) for a in kinds(acts, "add_item")}
    # plan() must not mutate its input: the mirror's own "A" is still unliked, so the
    # second call below is a genuine test of the cutoff window, not of a stale mutation
    assert m.songs["A"].liked == 0
    m.deleted_memberships[0].deleted_at = reconcile._iso(NOW - timedelta(days=9))
    assert ("CU", "spotify:track:tA") not in {
        (a.playlist_id, a.uri) for a in kinds(reconcile.plan(m, live, NOW), "add_item")
    }


def test_inbox_fifo_and_exemption():
    m = base()
    items = [item(f"Z{i:02d}", added=f"2026-08-{i + 1:02d}T00:00:00.000Z") for i in range(3)]
    live = Live(
        {
            "IN": live_pl("IN", "new songs", items),
            "CU": live_pl("CU", "feel good", [item("A")]),
            "SM": live_pl("SM", "pop", [item("A"), item("B")]),
        },
        {"A": item("A"), "B": item("B")},
        {},
    )
    acts = reconcile.plan(m, live, NOW, inbox_cap=2)
    assert [a.uri for a in kinds(acts, "remove_item") if a.playlist_id == "IN"] == [
        "spotify:track:tZ00"
    ]
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
            "CU": live_pl("CU", "feel good", [item("A")]),
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


def test_unplayable_relink_or_flag():
    m = base()
    m.songs["A"].spotify_ids = ["tA2", "tA"]
    m.songs["B"].spotify_playable = 0
    live = Live(
        {
            "CU": live_pl(
                "CU", "feel good", [item("A", playable=False), item("B", playable=False)]
            ),
            "SM": live_pl("SM", "pop", []),
            "IN": live_pl("IN", "new songs", []),
        },
        {"A": item("A", "tA2"), "B": item("B", playable=False)},
        {},
    )
    acts = reconcile.plan(m, live, NOW)
    assert ("CU", "spotify:track:tA") in {
        (a.playlist_id, a.uri) for a in kinds(acts, "remove_item")
    }
    assert ("CU", "spotify:track:tA2") in {(a.playlist_id, a.uri) for a in kinds(acts, "add_item")}
    assert any(a.kind == "flag" and "B" in a.text for a in acts)


def test_duplicate_isrc_keeps_earliest():
    m = base()
    live = Live(
        {
            "CU": live_pl(
                "CU",
                "feel good",
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
    assert [a.uri for a in kinds(acts, "remove_item") if a.playlist_id == "CU"] == [
        "spotify:track:tA"
    ]
    assert not kinds(acts, "readd_item")  # different uris: no readd needed


def test_duplicate_isrc_same_uri_needs_readd():
    m = base()
    live = Live(
        {
            "CU": live_pl(
                "CU",
                "feel good",
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
    assert [a.uri for a in kinds(acts, "remove_item") if a.playlist_id == "CU"] == [
        "spotify:track:tA"
    ]
    assert [a.uri for a in kinds(acts, "readd_item") if a.playlist_id == "CU"] == [
        "spotify:track:tA"
    ]


def test_ephemeral_expiry_and_skipped_playlists():
    m = base()
    m.playlists["SM"].pinned, m.playlists["SM"].expires_at = (
        0,
        reconcile._iso(NOW - timedelta(hours=1)),
    )
    live = Live(
        {
            "SM": live_pl("SM", "pop", None),
            "CU": live_pl("CU", "feel good", None),
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
            "CU": live_pl("CU", "feel good", [item(None)]),
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


def test_inbox_exempt_from_relink_and_dedupe():
    m = base()
    m.songs["A"].spotify_ids = ["tA2", "tA"]
    items = [
        item("A", "tA", added=T, playable=False),
        item("A", "tA9", added="2026-08-31T00:00:00.000Z"),
    ]
    live = Live(
        {
            "IN": live_pl("IN", "new songs", items),
            "CU": live_pl("CU", "feel good", []),
            "SM": live_pl("SM", "pop", []),
        },
        {"A": item("A")},
        {},
    )
    acts = reconcile.plan(m, live, NOW)
    assert not [a for a in kinds(acts, "remove_item") if a.playlist_id == "IN"]
    assert not [a for a in kinds(acts, "add_item") if a.playlist_id == "IN"]
    assert not [a for a in kinds(acts, "flag") if a.reason == "attention"]


def test_new_unliked_curated_song_has_one_edge_created_row_1():
    m = base()
    live = Live(
        {
            "CU": live_pl("CU", "feel good", [item("A"), item("E")]),
            "SM": live_pl("SM", "pop", [item("A"), item("B")]),
            "IN": live_pl("IN", "new songs", []),
        },
        {"A": item("A"), "B": item("B")},
        {},
    )
    acts = reconcile.plan(m, live, NOW)
    edges = [a for a in kinds(acts, "edge") if a.row["to_ref"] == "E"]
    assert len(edges) == 1 and edges[0].row["detail"]["created_row"] == 1
    assert [a.uri for a in kinds(acts, "like") if a.isrc == "E"] == ["spotify:track:tE"]


def test_apply_order():
    m = base()
    m.songs["B"].deezer_genres = ["Rock"]
    live = Live(
        {
            "CU": live_pl("CU", "feel good", [item("A"), item("C")]),
            "SM": live_pl("SM", "pop", [item("A"), item("B")]),
            "IN": live_pl("IN", "new songs", []),
        },
        {"A": item("A"), "B": item("B")},
        {},
    )
    order = [a.kind for a in reconcile.plan(m, live, NOW)]
    rank = {k: i for i, k in enumerate(reconcile.ORDER)}
    assert order == sorted(order, key=lambda k: rank[k])
