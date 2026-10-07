from datetime import timedelta

from core import reconcile
from core.model import Live, Membership
from tests.test_reconcile import NOW, T, base, item, kinds, live_pl, pl


def unliked_live():
    return Live({"CU": live_pl("CU", "curated", [item("A")])}, {}, {})


def test_explicit_unlike_preserves_curated_membership_and_creates_one_review():
    acts = reconcile.plan(base(), unliked_live(), NOW)
    assert not [a for a in acts if a.playlist_id == "CU" and a.kind == "remove_item"]
    assert not [
        a for a in acts if a.row and a.row.get("id") == "CU:A" and a.kind == "delete_membership"
    ]
    assert [a.isrc for a in kinds(acts, "review_exception")] == ["A"]
    assert not kinds(acts, "like")


def test_prior_unlike_exception_blocks_a_later_curated_add():
    m = base()
    m.songs["A"].liked = 0
    m.playlists["OTHER"] = pl("OTHER", "other", "curated")
    live = unliked_live()
    live.playlists["OTHER"] = live_pl("OTHER", "other", [item("A")])
    acts = reconcile.plan(m, live, NOW + timedelta(days=1), review_exceptions={"A"})
    assert not kinds(acts, "like")
    assert not kinds(acts, "review_exception")


def test_first_observed_unliked_curated_is_not_an_explicit_unlike():
    m = base()
    m.songs["A"].liked = 0
    acts = reconcile.plan(m, unliked_live(), NOW)
    assert not kinds(acts, "review_exception")
    assert not kinds(acts, "like")


def test_previously_liked_but_not_curated_is_not_a_curated_unlike():
    m = base()
    m.memberships.pop(("CU", "A"))
    acts = reconcile.plan(m, unliked_live(), NOW)
    assert not kinds(acts, "review_exception")
    assert not kinds(acts, "like")


def test_one_time_like_all_respects_prior_unlike_exceptions():
    m = base()
    m.songs["A"].liked = 0
    m.memberships[("CU", "C")] = Membership("CU", "C", "tC", T)
    live = Live({"CU": live_pl("CU", "curated", [item("A"), item("C")])}, {}, {})
    acts = reconcile.plan(m, live, NOW, review_exceptions={"A"}, like_existing_curated=True)
    assert [a.isrc for a in kinds(acts, "like")] == ["C"]


def test_reliking_does_not_restore_an_intentionally_deleted_curated_membership():
    m = base()
    m.songs["A"].liked = 0
    m.memberships.pop(("CU", "A"))
    m.deleted_memberships.append(Membership("CU", "A", "tA", T, "2026-09-07T00:00:00Z"))
    acts = reconcile.plan(m, Live({"CU": live_pl("CU", "curated", [])}, {"A": item("A")}, {}), NOW)
    assert not kinds(acts, "add_item")


def test_review_must_be_durable_before_consuming_unlike_baseline():
    from core.actions import apply
    from tests.test_actions import FakeHub, FakeSpotify

    hub, sp = FakeHub(), FakeSpotify()
    plan = reconcile.plan(base(), unliked_live(), NOW)
    out = apply(plan, sp, hub, False)
    assert out.errors and not hub.pushed and not sp.calls
    saved = []
    out = apply(plan, sp, hub, False, save_review=lambda row: saved.append(row))
    assert not out.errors and [r["id"] for r in saved] == ["A"]
    assert any(table == "songs" for table, _ in hub.pushed)


def test_review_dry_run_has_no_state_write():
    from core.actions import apply
    from tests.test_actions import FakeHub, FakeSpotify

    def forbidden(row):
        raise AssertionError("dry run wrote a review")

    out = apply(
        reconcile.plan(base(), unliked_live(), NOW),
        FakeSpotify(),
        FakeHub(),
        True,
        save_review=forbidden,
    )
    assert not out.errors and not out.applied


def test_unlike_exception_survives_restart_and_new_curated_membership(settings, monkeypatch):
    import gzip
    import json
    from core import archive, run, review_state
    from tests.test_release_regressions import Store, Spotify, A

    objects = {}
    monkeypatch.setattr(archive, "get", lambda s, k: objects.get(k))
    monkeypatch.setattr(archive, "put", lambda s, k, v: objects.__setitem__(k, v))
    monkeypatch.setattr(run.flags, "file", lambda *a: None)
    hub, sp = Store(), Spotify(liked=False)
    first = run.reconcile(settings, spotify=sp, hub=hub, now=NOW)
    assert not first.errors and not sp.calls
    state = json.loads(gzip.decompress(objects[review_state.key(settings)]))
    assert list(state) == [A]
    assert not hub.tables["playlist_songs"][f"P:{A}"].get("deleted_at")
    hub.tables["playlist_songs"].clear()  # The next pull discovers a curated membership anew.
    second = run.reconcile(settings, spotify=sp, hub=hub, now=NOW + timedelta(days=1))
    assert not second.errors and not sp.calls
    assert not [a for a in second.planned if a["kind"] == "review_exception"]
    assert json.loads(gzip.decompress(objects[review_state.key(settings)])) == state
