"""Rollout package: exact edits, owner decisions, simulation and serialized apply."""

import gzip
import json
import random
from datetime import datetime, timezone

import pytest

from core import curation, package, reconcile
from core.mirror import live_from_raw
from core.model import Mirror, Playlist, Song

NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
ME = "me"


def track(tid, isrc, name="t", playable=True, album="A", year="2001"):
    return {
        "id": tid,
        "uri": f"spotify:track:{tid}",
        "name": name,
        "is_local": False,
        "is_playable": playable,
        "external_ids": {"isrc": isrc},
        "duration_ms": 1000,
        "artists": [{"name": "x"}],
        "album": {"name": album, "release_date": year},
    }


def row(tid, isrc, added="2024-10-14T01:34:00Z", **kw):
    return {"added_at": added, "item": track(tid, isrc, **kw)}


def uri(tid):
    return f"spotify:track:{tid}"


# --- edit engine --------------------------------------------------------------------


def test_repeated_uri_is_deleted_and_the_kept_occurrence_reinserted_in_place():
    before = [uri("a"), uri("b"), uri("a"), uri("c")]
    after = [uri("a"), uri("b"), uri("c")]
    ops = package.edit_ops(before, after)
    assert ops == [
        {"op": "delete", "uris": [uri("a")]},
        {"op": "insert", "position": 0, "uris": [uri("a")]},
    ]
    seen = package.states(before, ops)
    assert seen[-1] == after
    assert [package.progress(state, before, ops) for state in seen] == [0, 1, 2]
    assert package.progress([uri("z")], before, ops) is None


def test_reorder_or_duplication_is_refused():
    with pytest.raises(package.PackageError):
        package.edit_ops([uri("a"), uri("b")], [uri("b"), uri("a")])
    with pytest.raises(package.PackageError):
        package.edit_ops([uri("a"), uri("b")], [uri("a"), uri("a"), uri("b")])


def test_random_removals_and_insertions_always_reach_the_target():
    rng = random.Random(7)
    for _ in range(300):
        before = [uri(rng.choice("abcdefgh")) for _ in range(rng.randint(0, 14))]
        keep = [u for u in before if rng.random() > 0.3]
        after = list(keep)
        for new in ("x", "y"):
            if rng.random() > 0.5:
                after.insert(rng.randint(0, len(after)), uri(new))
        try:
            ops = package.edit_ops(before, after)
        except package.PackageError:
            continue  # a removal of one occurrence that changes relative order
        assert package.states(before, ops)[-1] == after


# --- build ---------------------------------------------------------------------------

G, S, N = "Gpid", "Spid", "Npid"
ARCHIVE = "raw/spotify-pull/2026-10-06.json.gz"


def raw_observation():
    return {
        "playlists": [
            {"id": G, "name": "older", "owner": {"id": ME}, "snapshot_id": "g1"},
            {"id": S, "name": "captures", "owner": {"id": ME}, "snapshot_id": "s1"},
            {"id": N, "name": "new songs", "owner": {"id": ME}, "snapshot_id": "n1"},
            {"id": "ACL", "name": "ACL", "owner": {"id": ME}, "snapshot_id": "a1"},
        ],
        "items": {
            G: [
                row("orig", "GBORI0000001", added="2024-10-14T01:34:00Z"),
                row("remaster", "GBREM0000001", added="2018-12-25T02:26:31Z"),
                row("dup", "USDUP0000001"),
                row("dup", "USDUP0000001"),
                row("asked1", "USASK0000001"),
                row("asked2", "USASK0000002"),
            ],
            S: [
                row("rep", "USREP0000001", added="2019-01-01T00:00:00Z"),
                row("gone", "USGON0000001", added="2020-01-01T00:00:00Z", playable=False),
                row("rep", "USREP0000001", added="2021-01-01T00:00:00Z"),
                row("exc", "USEXC0000001", playable=False),
                row("alias1", "USALI0000001"),
                row("plain", "USPLA0000001"),
            ],
            N: [row("inbox", "USINB0000001")],
            "ACL": [row("acl", "USACL0000001")],
        },
        "liked": [
            {"added_at": "2020-01-01T00:00:00Z", "track": track("remaster", "GBREM0000001")},
            {"added_at": "2020-01-01T00:00:00Z", "track": track("alias2", "USALI0000002")},
            {"added_at": "2020-01-01T00:00:00Z", "track": track("orig2", "USSAM0000001")},
        ],
    }


def mirror_rows():
    playlists = {
        G: Playlist(G, "older", "curated", None, None, "g1", 1, None),
        S: Playlist(S, "captures", "curated", None, None, "s1", 1, None),
        N: Playlist(N, "new songs", "inbox", None, None, "n1", 1, None),
    }
    songs = {
        "GBORI0000001": Song("GBORI0000001", 0, None, "x", first_year=1974),
        "GBREM0000001": Song("GBREM0000001", 1, "2020-01-01T00:00:00Z", "x", first_year=2011),
    }
    return Mirror(songs, playlists, {}, [], set())


def occ(pid, index, tid, added="2024-10-14T01:34:00Z"):
    return {
        "playlist_id": pid,
        "index": index,
        "pointer": f"/items/{pid}/{index}",
        "archive": ARCHIVE,
        "spotify_id": tid,
        "added_at": added,
    }


def decisions():
    return {
        "identical_id_groups": [
            {
                "isrc": "USREP0000001",
                "title": "rep",
                "artists": ["x"],
                "playlist_id": S,
                "occurrences": [
                    occ(S, 0, "rep", "2019-01-01T00:00:00Z"),
                    occ(S, 2, "rep", "2021-01-01T00:00:00Z"),
                ],
                "keeper": occ(S, 0, "rep", "2019-01-01T00:00:00Z"),
                "remove": [occ(S, 2, "rep", "2021-01-01T00:00:00Z")],
                "status": "approved Oct 8 (rule a)",
            },
            {
                "isrc": "USDUP0000001",
                "title": "dup",
                "artists": ["x"],
                "playlist_id": G,
                "occurrences": [occ(G, 2, "dup"), occ(G, 3, "dup")],
                "keeper": occ(G, 2, "dup"),
                "remove": [occ(G, 3, "dup")],
                "status": "approved Oct 8 (rule a)",
            },
        ],
        "variant_groups": [
            {
                "isrc": ["GBORI0000001", "GBREM0000001"],
                "title": "Killer",
                "artists": ["x"],
                "playlist_id": G,
                "group_kind": "different_isrc_same_title_artist",
                "occurrences": [occ(G, 0, "orig"), occ(G, 1, "remaster", "2018-12-25T02:26:31Z")],
                "keeper": occ(G, 0, "orig"),
                "remove": [occ(G, 1, "remaster", "2018-12-25T02:26:31Z")],
                "rule_applied": "album-first keeper",
                "status": "auto_album_first",
            },
            {
                "isrc": ["USASK0000001", "USASK0000002"],
                "title": "Asked",
                "artists": ["x"],
                "playlist_id": G,
                "occurrences": [occ(G, 4, "asked1"), occ(G, 5, "asked2")],
                "keeper": occ(G, 4, "asked1"),
                "remove": [occ(G, 5, "asked2")],
                "status": "ask",
                "ask_id": "B1",
            },
        ],
        "unavailable_replacements": [
            {
                "isrc": "USGON0000001",
                "title": "gone",
                "artists": ["x"],
                "occurrences": [occ(S, 1, "gone", "2020-01-01T00:00:00Z")],
                "keeper": {"spotify_id": "back", "isrc": "USGON0000001", "duration_ms": 900},
                "status": "approved Oct 8 (b)",
            }
        ],
        "unavailable_exceptions": [
            {"isrc": "USEXC0000001", "spotify_ids": ["exc"], "rule_applied": "kept"}
        ],
        "flag_exceptions": [],
    }


def spec():
    return {
        "smart": [
            {"name": "older", "rule": {"v": 1, "first_year": {"lt": 2000}}, "replaces": G},
            {"name": "newer", "rule": {"v": 1, "first_year": {"gte": 2000}}},
        ],
        "classify": {"ACL": "curated"},
        "like_targets": {"USALI0000001": "alias1"},
    }


def built():
    m = mirror_rows()
    live = live_from_raw(raw_observation(), ME, m)
    return m, live, package.build(live, m, decisions(), spec(), observed_at=NOW.isoformat())


def test_build_keeps_rollback_copy_untouched_and_edits_other_curated_playlists():
    _, _, out = built()
    p, report = out["package"], out["report"]
    assert [e["playlist_id"] for e in p["playlists"]] == [S]
    edit = p["playlists"][0]
    # Repeat removed (kept earliest), unavailable occurrence replaced in place.
    assert edit["after"] == [uri("rep"), uri("back"), uri("exc"), uri("alias1"), uri("plain")]
    assert package.states(edit["before"], edit["ops"])[-1] == edit["after"]
    assert {g["group"] for g in report["frozen_rollback_groups"]} == {"dup / x", "Killer / x"}
    assert p["renames"] == [{"playlist_id": G, "from": "older", "to": "older (pre-sync)"}]
    assert p["classify"] == [{"playlist_id": "ACL", "kind": "curated", "name": "ACL"}]


def test_build_normalizes_likes_across_recordings():
    _, _, out = built()
    p, report = out["package"], out["report"]
    likes = {like["uri"]: like["isrc"] for like in p["likes"]}
    # Superseded remaster is liked: like the original album keeper, then unlike it.
    assert likes[uri("orig")] == "GBORI0000001"
    assert {(u["uri"], u["keeper"]) for u in p["unlikes"]} >= {(uri("remaster"), uri("orig"))}
    # Replacement target, selected alias, classified playlist and plain songs are liked.
    assert likes[uri("back")] == "USGON0000001"
    assert likes[uri("alias1")] == "USALI0000001"
    assert likes[uri("acl")] == "USACL0000001"
    assert likes[uri("plain")] == "USPLA0000001"
    # Accepted unavailable exception is never liked; asked groups wait for Alex.
    assert uri("exc") not in likes and uri("asked1") not in likes and uri("asked2") not in likes
    # The accepted exception is reported as an exception, not as an open question.
    assert "USEXC0000001" not in {h.get("isrc") for h in report["held"]}
    assert "USEXC0000001" in {e["isrc"] for e in report["exceptions"]}
    assert {h["isrc"] for h in report["held"] if h.get("isrc")} >= {"USASK0000001", "USASK0000002"}
    assert uri("inbox") not in likes
    assert set(p["tracks"]) >= set(likes)


def test_moved_occurrence_is_reported_stale_not_guessed():
    m = mirror_rows()
    raw = raw_observation()
    raw["items"][S].insert(0, row("new", "USNEW0000001", added="2026-10-08T00:00:00Z"))
    raw["items"][S].append(row("rep", "USREP0000001", added="2021-01-01T00:00:00Z"))
    live = live_from_raw(raw, ME, m)
    out = package.build(live, m, decisions(), spec(), observed_at=NOW.isoformat())
    assert any(s["group"] == "rep / x" for s in out["report"]["stale"])


# --- simulation through the real planner ---------------------------------------------


def test_simulation_shows_smart_materialization_after_the_package():
    m, live, out = built()
    p = out["package"]
    m2, live2 = package.simulate(p, m, live, ME, NOW)
    assert live2.playlists[G].name == "older (pre-sync)"
    assert m2.playlists["planned:older"].kind == "smart"
    assert [it.uri for it in live2.playlists[S].items] == p["playlists"][0]["after"]
    assert "GBORI0000001" in live2.liked and "GBREM0000001" not in live2.liked
    plan = reconcile.plan(m2, live2, NOW, today="2026-10-09")
    adds = {(a.playlist_id, a.isrc) for a in plan if a.kind == "add_item"}
    assert ("planned:older", "GBORI0000001") in adds
    assert not [a for a in plan if a.kind == "remove_item" and a.playlist_id in (G, S)]


def test_validate_refuses_changed_playlists_and_name_collisions():
    m, live, out = built()
    p = out["package"]
    assert package.validate(p, live, {}) == []
    raw = raw_observation()
    raw["items"][S].append(row("late", "USLAT0000001"))
    raw["playlists"].append({"id": "X", "name": "newer", "owner": {"id": ME}, "snapshot_id": "x"})
    raw["items"]["X"] = []
    problems = package.validate(p, live_from_raw(raw, ME, m), {})
    assert any(S in problem for problem in problems)
    assert any("newer" in problem for problem in problems)


# --- apply -------------------------------------------------------------------------------


class FakeSpotify:
    def __init__(self, raw, extra_tracks):
        self.playlists = {p["id"]: dict(p) for p in raw["playlists"]}
        self.items = {pid: list(rows) for pid, rows in raw["items"].items()}
        self.liked = list(raw["liked"])
        self.catalog = {}
        for rows in [*self.items.values(), self.liked]:
            for r in rows:
                t = r.get("item") or r.get("track")
                self.catalog[t["uri"]] = t
        for u, meta in extra_tracks.items():
            self.catalog.setdefault(u, package._raw_track(meta, u))
        self.calls = []
        self.fail_on = None

    def _call(self, name):
        self.calls.append(name)
        if self.fail_on == len(self.calls):
            raise RuntimeError("lost response")

    def me(self):
        return {"id": ME}

    def get_playlists(self):
        return list(self.playlists.values())

    def get_playlist_items(self, pid, market):
        return list(self.items[pid])

    def get_liked(self, market):
        return list(self.liked)

    def rename_playlist(self, pid, name):
        self.playlists[pid]["name"] = name
        self._call("rename")

    def create_playlist(self, name, description, public=False):
        pid = f"new-{len(self.playlists)}"
        self.playlists[pid] = {"id": pid, "name": name, "owner": {"id": ME}, "snapshot_id": "0"}
        self.items[pid] = []
        self._call("create")
        return {"id": pid}

    def add_items(self, pid, uris, position=None):
        rows = [{"added_at": "2026-10-09T12:00:00Z", "item": self.catalog[u]} for u in uris]
        at = len(self.items[pid]) if position is None else position
        self.items[pid][at:at] = rows
        self._call("add")

    def remove_items(self, pid, uris):
        self.items[pid] = [r for r in self.items[pid] if r["item"]["uri"] not in set(uris)]
        self._call("remove")

    def like(self, uris):
        for u in uris:
            self.liked.insert(0, {"added_at": "2026-10-09T12:00:00Z", "track": self.catalog[u]})
        self._call("like")

    def unlike(self, uris):
        self.liked = [r for r in self.liked if r["track"]["uri"] not in set(uris)]
        self._call("unlike")


class FakeHub:
    def __init__(self, m):
        self.m = m
        self.pushed = []

    def push(self, table, rows):
        self.pushed.append((table, rows))


@pytest.fixture
def store(mocker):
    data = {}
    mocker.patch("core.package.archive.put", side_effect=lambda s, k, v: data.__setitem__(k, v))
    mocker.patch("core.package.archive.get", side_effect=lambda s, k: data.get(k))
    return data


def run_apply(settings, mocker, spotify, p, state, m):
    mocker.patch("core.package.mirror_mod.load_mirror", return_value=m)
    return package.apply(p, spotify, FakeHub(m), settings, NOW, state, lambda: None)


def test_apply_executes_verifies_and_keeps_rollback_copy(settings, mocker, store):
    m, live, out = built()
    p = out["package"]
    spotify = FakeSpotify(raw_observation(), p["tracks"])
    older_before = [r["item"]["uri"] for r in spotify.items[G]]
    receipt = run_apply(settings, mocker, spotify, p, {}, m)
    assert receipt["verified"], receipt
    assert spotify.playlists[G]["name"] == "older (pre-sync)"
    assert [r["item"]["uri"] for r in spotify.items[G]] == older_before
    assert [r["item"]["uri"] for r in spotify.items[S]] == p["playlists"][0]["after"]
    names = {pl["name"] for pl in spotify.playlists.values()}
    assert {"older", "newer"} <= names
    saved = {r["track"]["uri"] for r in spotify.liked}
    assert uri("orig") in saved and uri("remaster") not in saved
    state = json.loads(gzip.decompress(store[curation.state_key("default")]))
    # Normalization is not an owner unlike: no exception follows on the next observation.
    liked_now = {r["track"]["external_ids"]["isrc"] for r in spotify.liked}
    members = {(pid, "GBREM0000001") for pid in (G,)}
    _, nxt = curation.advance(state, liked_now, members, "next")
    assert nxt["exceptions"] == {}


def test_apply_resumes_after_lost_response_without_repeating_work(settings, mocker, store):
    m, live, out = built()
    p = out["package"]
    spotify = FakeSpotify(raw_observation(), p["tracks"])
    spotify.fail_on = 3  # rename, create, then the second create's response is lost
    state = {}
    with pytest.raises(RuntimeError):
        run_apply(settings, mocker, spotify, p, state, m)
    spotify.fail_on = None
    receipt = run_apply(settings, mocker, spotify, p, state, m)
    assert receipt["verified"], receipt
    names = [pl["name"] for pl in spotify.playlists.values()]
    assert names.count("newer") == 1 and names.count("older") == 1
    assert spotify.calls.count("create") == 2


def test_apply_refuses_before_any_write_when_inputs_changed(settings, mocker, store):
    m, live, out = built()
    raw = raw_observation()
    raw["items"][S].append(row("late", "USLAT0000001"))
    spotify = FakeSpotify(raw, out["package"]["tracks"])
    receipt = run_apply(settings, mocker, spotify, out["package"], {}, m)
    assert receipt["applied"] is False and receipt["problems"]
    assert spotify.calls == []


def test_user_unlike_before_the_package_still_becomes_an_exception():
    live_after = type("L", (), {"liked": {"A": 1}, "playlists": {}})()
    state = {
        "version": 1,
        "pending_likes": [],
        "baseline": {
            "source_ref": "r",
            "liked": ["A", "B"],
            "curated": [["P", "B"]],
            "own_likes": [],
        },
        "exceptions": {},
    }
    adjusted = package.adjust_curation(state, {"likes": [], "unlikes": []}, live_after, {})
    _, nxt = curation.advance(adjusted, {"A"}, {("P", "B")}, "next")
    assert "B" in nxt["exceptions"]


def test_dry_run_with_package_previews_the_first_reconciliation_after_it(settings, mocker):
    from core import run

    m, live, out = built()
    p = out["package"]
    mocker.patch("core.run.archive.get", return_value=None)
    put = mocker.patch("core.run.archive.put")
    mocker.patch("core.run.mirror.load_mirror", return_value=m)
    mocker.patch("core.run.mirror.pull_live", return_value=live)
    filed = mocker.patch("core.run.flags.file")

    class Me:
        def me(self):
            return {"id": ME}

    log = run.reconcile(settings, dry_run=True, now=NOW, spotify=Me(), hub=object(), package=p)
    adds = {(a["playlist_id"], a["isrc"]) for a in log.planned if a["kind"] == "add_item"}
    assert ("planned:older", "GBORI0000001") in adds
    assert not [a for a in log.planned if a["kind"] == "like"]  # the package already liked
    assert log.snapshot["package"]["digest"] == package.digest(p)
    assert log.snapshot["spotify"] is live.raw and log.snapshot["me"] == ME
    put.assert_not_called()
    filed.assert_not_called()
    with pytest.raises(ValueError):
        run.reconcile(settings, dry_run=False, now=NOW, spotify=Me(), hub=object(), package=p)
