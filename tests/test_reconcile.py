from datetime import datetime, timedelta, timezone

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


def test_undo_restores_curated_within_window():
    m = base()
    m.songs["A"].liked = 0
    m.memberships.pop(("CU", "A"))
    m.deleted_memberships.append(
        Membership("CU", "A", "tA", T, deleted_at=(NOW - timedelta(days=2)).isoformat())
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
    m.deleted_memberships[0].deleted_at = (NOW - timedelta(days=9)).isoformat()
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
        {"A": item("A"), "B": item("B")},
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


def test_ephemeral_expiry_and_skipped_playlists():
    m = base()
    m.playlists["SM"].pinned, m.playlists["SM"].expires_at = (
        0,
        (NOW - timedelta(hours=1)).isoformat(),
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
