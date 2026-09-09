from core import actions
from core.model import Action


class FakeSpotify:
    def __init__(self, fail_playlist=None):
        self.calls, self.fail = [], fail_playlist

    def like(self, uris):
        self.calls.append(("like", tuple(uris)))

    def add_items(self, pid, uris):
        if pid == self.fail:
            raise RuntimeError("boom")
        self.calls.append(("add", pid, tuple(uris)))

    def remove_items(self, pid, uris):
        self.calls.append(("remove", pid, tuple(uris)))

    def set_description(self, pid, text):
        self.calls.append(("desc", pid, text))

    def unfollow_playlist(self, pid):
        self.calls.append(("unfollow", pid))


class FakeHub:
    def __init__(self):
        self.pushed = []

    def push(self, table, rows):
        self.pushed.append((table, rows))
        return {"upserted": len(rows), "rejected": []}


ACTS = [
    Action("like", uri="spotify:track:1"),
    Action("like", uri="spotify:track:2"),
    Action("add_item", playlist_id="P", uri="spotify:track:1"),
    Action("add_item", playlist_id="P", uri="spotify:track:3"),
    Action("add_item", playlist_id="Q", uri="spotify:track:1"),
    Action("remove_item", playlist_id="P", uri="spotify:track:9"),
    Action("set_description", playlist_id="P", text="smart · synced d"),
    Action("upsert_song", row={"id": "A", "liked": 1}),
    Action("upsert_membership", row={"id": "P:A", "playlist_id": "P"}),
    Action("delete_membership", row={"id": "P:B", "deleted_at": "t"}),
    Action("edge", row={"id": "like:liked:A"}),
    Action("upsert_playlist", row={"id": "P", "name": "p"}),
    Action("flag", text="something odd", reason="attention"),
]


def test_apply_batches_spotify_and_groups_hub_tables():
    sp, hub = FakeSpotify(), FakeHub()
    log = actions.apply(ACTS, sp, hub, dry_run=False)
    assert ("like", ("spotify:track:1", "spotify:track:2")) in sp.calls
    assert ("add", "P", ("spotify:track:1", "spotify:track:3")) in sp.calls and (
        "add",
        "Q",
        ("spotify:track:1",),
    ) in sp.calls
    assert ("remove", "P", ("spotify:track:9",)) in sp.calls and (
        "desc",
        "P",
        "smart · synced d",
    ) in sp.calls
    tables = {t: rows for t, rows in hub.pushed}
    assert [r["id"] for r in tables["songs"]] == ["A"]
    assert {r["id"] for r in tables["playlist_songs"]} == {"P:A", "P:B"}
    assert tables["provenance"][0]["id"] == "like:liked:A" and tables["playlists"][0]["id"] == "P"
    assert log.flags == ["something odd"] and log.applied["add_item"] == 3 and not log.errors


def test_dry_run_touches_nothing_but_counts():
    sp, hub = FakeSpotify(), FakeHub()
    log = actions.apply(ACTS, sp, hub, dry_run=True)
    assert sp.calls == [] and hub.pushed == [] and log.dry_run
    assert log.applied == {} and len(log.planned) == len(ACTS)


def test_spotify_error_stops_dependent_writes():
    sp, hub = FakeSpotify(fail_playlist="P"), FakeHub()
    log = actions.apply(ACTS, sp, hub, dry_run=False)
    assert ("add", "Q", ("spotify:track:1",)) not in sp.calls
    assert hub.pushed == [] and log.errors


def test_readd_item_after_remove_for_same_playlist_and_uri():
    sp, hub = FakeSpotify(), FakeHub()
    acts = [
        Action("remove_item", playlist_id="P", uri="spotify:track:1"),
        Action("readd_item", playlist_id="P", uri="spotify:track:1"),
    ]
    log = actions.apply(acts, sp, hub, dry_run=False)
    assert sp.calls.index(("remove", "P", ("spotify:track:1",))) < sp.calls.index(
        ("add", "P", ("spotify:track:1",))
    )
    assert log.applied["readd_item"] == 1


def test_writes_false_skips_spotify_but_still_applies_hub_and_flags():
    sp, hub = FakeSpotify(), FakeHub()
    log = actions.apply(ACTS, sp, hub, dry_run=False, writes=False)
    assert sp.calls == []
    assert len(log.skipped) == 7
    assert log.applied["add_item"] == 0 and log.flags == ["something odd"]
    tables = {t for t, _ in hub.pushed}
    assert tables == {"songs", "playlists", "playlist_songs", "provenance"}


def test_hub_error_is_recorded_not_raised():
    from core.hub import HubError

    class FailingHub(FakeHub):
        def push(self, table, rows):
            if table == "songs":
                raise HubError("boom")
            return super().push(table, rows)

    hub = FailingHub()
    log = actions.apply(ACTS, FakeSpotify(), hub, dry_run=False)
    assert log.errors and "boom" in log.errors[0]
    tables = {t for t, _ in hub.pushed}
    assert tables == set()


def test_pending_preserves_explicit_timestamps_and_null_through_rejection():
    import copy
    import json

    import httpx

    from core.hub import Hub

    supplied = "2026-09-08T10:11:12.987Z"
    rows = [
        {"id": "A", "updated_at": supplied, "liked": 1},
        {"id": "B", "updated_at": None, "liked": 0},
        {"id": "C", "liked": 0},
    ]
    plan = [Action("upsert_song", row=row) for row in rows]
    original = copy.deepcopy(rows)
    saved, bodies = [], []

    def checkpoint(ops):
        saved.append(json.loads(json.dumps(ops)))

    def handler(req):
        body = json.loads(req.content)
        bodies.append(body)
        return httpx.Response(
            200,
            json={"upserted": 2, "rejected": [{"id": "B", "rule": "updated_at required"}]},
        )

    hub = Hub("https://hub.test", "dummy", httpx.Client(transport=httpx.MockTransport(handler)))
    out = actions.apply(plan, FakeSpotify(), hub, False, checkpoint=checkpoint)
    assert out.errors and "updated_at required" in out.errors[0]
    assert len(saved) == 1  # A rejected operation stays pending.
    assert saved[0][0]["rows"][:2] == original[:2]
    assert saved[0][0]["rows"][2].get("updated_at") is not None
    pending = copy.deepcopy(saved[0])
    out = actions.apply(plan, FakeSpotify(), hub, False, checkpoint=checkpoint, pending=pending)
    assert out.errors
    assert bodies[0]["rows"] == bodies[1]["rows"] == saved[0][0]["rows"]
    assert pending == saved[0] and rows == original


def test_dry_run_does_not_prepare_or_checkpoint_pending(mocker):
    clock = mocker.patch.object(actions, "datetime", create=True)
    clock.now.side_effect = AssertionError("dry run must not generate a timestamp")
    sp, hub = FakeSpotify(), FakeHub()
    pending = [{"kind": "hub", "table": "songs", "rows": [{"id": "A"}], "counts": {}}]

    def checkpoint(ops):
        raise AssertionError("dry run must not save")

    for intent in (None, pending):
        out = actions.apply(ACTS, sp, hub, True, checkpoint=checkpoint, pending=intent)
        assert out.dry_run and not out.errors and not out.applied
        assert sp.calls == [] and hub.pushed == []
    assert pending[0]["rows"] == [{"id": "A"}]
    assert clock.now.call_count == 0
