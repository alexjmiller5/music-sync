"""Approved curation behavior through the real reconcile service boundary."""

import copy

import pytest

from tests.test_release_regressions import (
    A,
    T,
    Store,
    Spotify,
    execute,
    raw,
    archive_store as archive_store,
)


@pytest.mark.parametrize("skipped", [False, True])
def test_unlike_preserves_curated_membership(settings, archive_store, skipped):
    hub, sp = Store(), Spotify(liked=False)
    if skipped:
        hub.tables["playlists"]["P"]["snapshot_id"] = str(sp.snapshot)
    original = copy.deepcopy(hub.tables["playlist_songs"])
    out = execute(settings, sp, hub)
    assert not out.errors
    assert sp.calls == []
    assert sp.items == [raw()]
    assert hub.tables["songs"][A]["liked"] == 0
    for key, row in original.items():
        assert all(hub.tables["playlist_songs"][key][col] == val for col, val in row.items())
    assert not execute(settings, sp, hub).errors
    assert sp.calls == []


def test_relike_does_not_restore_manually_removed_curated_song(settings, archive_store):
    hub, sp = Store(liked=0), Spotify(items=[])
    hub.tables["playlist_songs"][f"P:{A}"]["deleted_at"] = T
    out = execute(settings, sp, hub)
    assert not out.errors
    assert sp.calls == [] and sp.items == []
    assert hub.tables["playlist_songs"][f"P:{A}"]["deleted_at"] == T


def test_unlike_preview_preserves_curated_and_has_zero_writes(settings, archive_store):
    hub, sp = Store(), Spotify(liked=False)
    original = copy.deepcopy(hub.tables)
    out = execute(settings, sp, hub, dry_run=True)
    assert not out.errors
    assert not any(a["kind"] in {"like", "remove_item", "add_item"} for a in out.planned)
    assert hub.tables == original and sp.calls == [] and archive_store == {}


def test_bootstrap_is_preview_only_then_new_curated_song_auto_likes(settings, archive_store):
    hub, sp = Store(liked=0), Spotify(liked=False)
    assert not execute(settings, sp, hub).errors
    assert sp.calls == []
    sp.items.append(raw("USAAA2600002", "b"))
    sp.snapshot += 1
    assert not execute(settings, sp, hub).errors
    assert [c for c in sp.calls if c[0] == "like"] == [("like", ["spotify:track:b"])]


def test_exception_survives_observation_import_and_another_curated_add(settings, archive_store):
    import gzip
    import json
    from core import curation

    hub, sp = Store(), Spotify()
    assert not execute(settings, sp, hub).errors
    sp.liked = []
    assert not execute(settings, sp, hub, writes=False).errors
    key = curation.state_key(settings.workspace)
    state = json.loads(gzip.decompress(archive_store[key]))
    assert set(state["exceptions"]) == {A}
    hub.tables["playlist_songs"].clear()  # unrelated mirror repair must not clear the exception
    sp.snapshot += 1
    assert not execute(settings, sp, hub).errors
    assert sp.calls == []
    assert json.loads(gzip.decompress(archive_store[key]))["exceptions"] == state["exceptions"]


def test_failed_hub_write_does_not_consume_curation_baseline(settings, archive_store):
    import gzip
    import json
    from core import curation

    hub, sp = Store(), Spotify()
    assert not execute(settings, sp, hub).errors
    key = curation.state_key(settings.workspace)
    before = archive_store[key]
    sp.liked = []
    hub.fail = "songs"
    assert execute(settings, sp, hub).errors
    assert archive_store[key] == before
    assert not execute(settings, sp, hub).errors
    assert A in json.loads(gzip.decompress(archive_store[key]))["exceptions"]


def test_new_unclassified_playlist_does_not_auto_like(settings, archive_store):
    hub, sp = Store(liked=0, member=False), Spotify(liked=False)
    hub.tables["playlists"].clear()
    assert not execute(settings, sp, hub).errors
    assert sp.calls == []


@pytest.mark.parametrize("items", [[raw(), raw()], [raw(playable=False)]])
def test_curated_duplicate_or_unplayable_alias_requires_review(settings, archive_store, items):
    hub, sp = Store(), Spotify(items=items)
    sp.liked = [raw(tid="replacement")]
    hub.tables["songs"][A]["spotify_ids"].append("replacement")
    out = execute(settings, sp, hub)
    assert not out.errors
    assert sp.calls == [] and sp.items == items
    assert out.flags


@pytest.mark.parametrize("writes", [False, True])
def test_duplicate_observations_do_not_choose_membership_alias(settings, archive_store, writes):
    hub, sp = Store(), Spotify(items=[raw(tid="other", added="2020-01-01T00:00:00.000Z"), raw()])
    original = copy.deepcopy(hub.tables["playlist_songs"][f"P:{A}"])
    assert not execute(settings, sp, hub, writes=writes).errors
    row = hub.tables["playlist_songs"][f"P:{A}"]
    assert all(row[k] == v for k, v in original.items())


def test_new_curated_duplicate_does_not_choose_auto_like_alias(settings, archive_store):
    hub, sp = Store(liked=0, member=False), Spotify(items=[], liked=False)
    assert not execute(settings, sp, hub).errors
    sp.items = [raw(), raw(tid="other")]
    sp.snapshot += 1
    assert not execute(settings, sp, hub).errors
    assert sp.calls == []
    assert f"P:{A}" not in hub.tables["playlist_songs"]


def test_observation_import_retains_new_curated_auto_like_intent(settings, archive_store):
    hub, sp = Store(liked=0, member=False), Spotify(items=[], liked=False)
    assert not execute(settings, sp, hub).errors
    sp.items = [raw()]
    sp.snapshot += 1
    assert not execute(settings, sp, hub, writes=False).errors
    assert sp.calls == []
    assert not execute(settings, sp, hub).errors
    assert sp.calls == [("like", ["spotify:track:a"])]


def test_smart_exclusion_holds_conflicting_aliases_for_review(settings, archive_store):
    hub = Store(kind="smart")
    hub.tables["playlists"]["P"]["rule"] = {"v": 1}
    sp = Spotify(items=[raw(tid="one"), raw(tid="two")], liked=False)
    out = execute(settings, sp, hub)
    assert not out.errors
    assert sp.calls == []
    assert len(sp.items) == 2
    assert hub.tables["playlist_songs"][f"P:{A}"]["deleted_at"] is None
    assert out.flags


def test_auto_like_does_not_choose_between_cross_playlist_aliases(settings, archive_store):
    class TwoPlaylists(Spotify):
        def get_playlists(self):
            return [
                *super().get_playlists(),
                {
                    "id": "Q",
                    "name": "Second",
                    "owner": {"id": "owner"},
                    "snapshot_id": str(self.snapshot),
                },
            ]

        def get_playlist_items(self, pid, market):
            return (
                super().get_playlist_items(pid, market)
                if pid == "P"
                else [raw(tid="other")]
                if self.items
                else []
            )

    hub, sp = Store(liked=0, member=False), TwoPlaylists(items=[], liked=False)
    hub.tables["playlists"]["Q"] = {**hub.tables["playlists"]["P"], "id": "Q"}
    assert not execute(settings, sp, hub).errors
    sp.items = [raw()]
    sp.snapshot += 1
    out = execute(settings, sp, hub)
    assert not out.errors and sp.calls == []
    assert any("alias" in flag for flag in out.flags)


def test_conflicting_observation_metadata_preserves_existing_release(settings, archive_store):
    hub, sp = Store(), Spotify()
    hub.tables["songs"][A].update(title="Reviewed title", album="Reviewed release", album_year=2001)
    other = raw(tid="other")
    other["item"].update(
        name="Different title", album={"name": "Other release", "release_date": "2020"}
    )
    sp.items[0]["item"]["album"] = {"name": "Original release", "release_date": "1990"}
    sp.items.append(other)
    out = execute(settings, sp, hub, writes=False)
    assert not out.errors
    row = hub.tables["songs"][A]
    assert (row["title"], row["album"], row["album_year"]) == (
        "Reviewed title",
        "Reviewed release",
        2001,
    )
    assert any("metadata" in flag for flag in out.flags)


def test_snapshot_history_keeps_each_original_occurrence(settings, archive_store):
    hub, sp = Store(), Spotify(items=[raw(), raw(added="2026-09-06T00:00:00.000Z")])
    out = execute(settings, sp, hub, dry_run=True)
    evidence = [a["row"] for a in out.planned if a["kind"] == "insert_edge"]
    assert len(evidence) == 2
    assert {r["detail"]["locator"] for r in evidence} == {"/items/P/0", "/items/P/1"}
    assert len({r["id"] for r in evidence}) == 2
    assert all(
        r["field"] is None and r["from_ref"].startswith("raw/spotify-pull/") for r in evidence
    )
    assert all(set(r["detail"]) == {"kind", "locator"} for r in evidence)


def test_history_is_insert_only_and_unchanged_pull_does_not_repeat_evidence(
    settings, archive_store
):
    import gzip
    import json

    hub, sp = Store(), Spotify()
    assert not execute(settings, sp, hub).errors
    original = {
        k: copy.deepcopy(v)
        for k, v in hub.tables["provenance"].items()
        if k.startswith("spotify-occurrence:")
    }
    assert len(original) == 1
    assert not execute(settings, sp, hub).errors
    assert {k for k in hub.tables["provenance"] if k.startswith("spotify-occurrence:")} == set(
        original
    )
    sp.items[0]["added_at"] = "2026-09-08T00:00:00.000Z"
    sp.snapshot += 1
    assert not execute(settings, sp, hub).errors
    assert all(hub.tables["provenance"][k] == v for k, v in original.items())
    assert len([k for k in hub.tables["provenance"] if k.startswith("spotify-occurrence:")]) == 2
    first = next(iter(original.values()))
    assert (
        json.loads(gzip.decompress(archive_store[first["from_ref"]]))["items"]["P"][0]["added_at"]
        == T
    )


def test_partial_history_insert_retry_preserves_ids_and_delays_baseline(settings, archive_store):
    import gzip
    import json
    from core import curation
    from core.hub import HubError

    class PartialStore(Store):
        def __init__(self):
            super().__init__()
            self.first = True
            self.batch_sizes = []

        def insert_rows(self, table, rows):
            self.batch_sizes.append(len(rows))
            if self.first:
                self.first = False
                super().insert_rows(table, rows[:1])
                raise HubError("response lost after partial insert")
            return super().insert_rows(table, rows)

    hub, sp = PartialStore(), Spotify(items=[raw()] * 201)
    assert execute(settings, sp, hub).errors
    assert curation.state_key(settings.workspace) not in archive_store
    original = {
        key: copy.deepcopy(row)
        for key, row in hub.tables["provenance"].items()
        if key.startswith("spotify-occurrence:")
    }
    assert len(original) == 1
    assert not execute(settings, sp, hub).errors
    assert all(hub.tables["provenance"][key] == row for key, row in original.items())
    assert (
        len([key for key in hub.tables["provenance"] if key.startswith("spotify-occurrence:")])
        == 201
    )
    assert max(hub.batch_sizes) <= 100
    assert json.loads(gzip.decompress(archive_store[curation.state_key(settings.workspace)]))[
        "occurrence_fingerprints"
    ]
