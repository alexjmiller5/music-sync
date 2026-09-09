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
