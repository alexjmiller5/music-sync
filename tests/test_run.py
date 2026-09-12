from datetime import datetime, timezone
from copy import deepcopy
import gzip
import json

from core import actions, run
from core.hub import HubError
from core.model import Action, Mirror, Live
from core.spotify_client import SpotifyAuthError

import pytest


@pytest.fixture(autouse=True)
def archive_read(mocker):
    mocker.patch("core.archive.get", return_value=None)


class DeadSpotify:
    def me(self):
        raise SpotifyAuthError("invalid_grant: revoked")


def test_invalid_grant_becomes_flag_and_stops(settings, mocker):
    filed = mocker.patch("core.run.flags.file", return_value="page")
    log = run.reconcile(
        settings,
        dry_run=True,
        now=datetime(2026, 9, 8, tzinfo=timezone.utc),
        spotify=DeadSpotify(),
        hub=object(),
    )
    assert log.errors and "invalid_grant" in log.errors[0]
    filed.assert_not_called()  # dry-run must never file a real task


class LiveSpotify:
    def me(self):
        return {"id": "me"}


class FailingHub:
    def push(self, table, rows):
        raise HubError("boom")


def test_hub_error_is_filed_with_a_full_runlog(settings, mocker):
    mocker.patch("core.run.mirror.load_mirror", return_value=Mirror({}, {}, {}, [], set()))
    mocker.patch("core.run.mirror.pull_live", return_value=Live({}, {}, {}))
    mocker.patch("core.run.archive.put")
    mocker.patch(
        "core.run.reconcile_mod.plan",
        return_value=[
            Action("flag", text="something odd"),
            Action("upsert_song", row={"id": "A", "liked": 1}),
        ],
    )
    filed = mocker.patch("core.run.flags.file", return_value="page")

    log = run.reconcile(
        settings,
        dry_run=False,
        now=datetime(2026, 9, 8, tzinfo=timezone.utc),
        spotify=LiveSpotify(),
        hub=FailingHub(),
    )

    assert log.flags == ["something odd"]
    assert log.errors and "boom" in log.errors[0]
    assert filed.call_args.args[2] == ["something odd"]
    assert any("boom" in e for e in filed.call_args.args[3])


def test_writes_false_is_passed_through_to_apply(settings, mocker):
    mocker.patch("core.run.mirror.load_mirror", return_value=Mirror({}, {}, {}, [], set()))
    mocker.patch("core.run.mirror.pull_live", return_value=Live({}, {}, {}))
    mocker.patch("core.run.archive.put")
    mocker.patch("core.run.reconcile_mod.plan", return_value=[])
    mocker.patch("core.run.flags.file")
    spy = mocker.patch("core.run.actions.apply", wraps=actions.apply)

    run.reconcile(
        settings,
        dry_run=False,
        now=datetime(2026, 9, 8, tzinfo=timezone.utc),
        spotify=LiveSpotify(),
        hub=FailingHub(),
        writes=False,
    )

    assert spy.call_args.kwargs["writes"] is False


@pytest.mark.parametrize("writes", [False, True])
@pytest.mark.parametrize("dry_run", [False, True])
def test_real_ingestion_keeps_metadata_archive_and_provenance_without_enrichment(
    settings, mocker, writes, dry_run
):
    from tests.test_release_regressions import Store, Spotify, A, raw

    hub, sp = Store(member=False), Spotify(items=[])
    hub.tables["songs"].clear()
    tr = raw()["item"]
    tr.update(
        album={"name": "Release", "release_date": "2019"},
        duration_ms=123456,
        linked_from={"id": "original"},
    )
    sp.liked = [{"added_at": "2020-01-01T00:00:00Z", "track": tr}]
    # Unexpected enrichment fails immediately; the real planner, normalizer and applicator run.
    mocker.patch.object(sp, "search_track", side_effect=AssertionError("unexpected enrichment"))
    before = deepcopy((hub.tables, sp.liked))
    saved = {}

    def put(settings, key, data):
        if key.startswith("raw/"):
            assert hub.tables == before[0] and sp.calls == []
        saved[key] = json.loads(gzip.decompress(data))

    mocker.patch("core.archive.put", side_effect=put)
    key = mocker.patch("core.archive.key_for", return_value="raw/spotify-pull/dummy.json.gz")
    mocker.patch("core.run.flags.file")
    out = run.reconcile(
        settings,
        spotify=sp,
        hub=hub,
        now=datetime(2026, 9, 12, 12, tzinfo=timezone.utc),
        writes=writes,
        dry_run=dry_run,
    )
    assert not out.errors
    row = next(a["row"] for a in out.planned if a["kind"] == "upsert_song")
    assert row["title"] == "Song" and row["album_year"] == 2019 and row["duration_ms"] == 123456
    assert row["spotify_ids"] == ["a", "original"]
    direct = [
        a["row"] for a in out.planned if a["kind"] == "edge" and a["row"]["rel"] == "evidence_of"
    ]
    assert direct and all(r["from_ref"] == "raw/spotify-pull/dummy.json.gz" for r in direct)
    assert all(r["detail"]["observed_at"] == "2026-09-12T12:00:00.000Z" for r in direct)
    assert key.call_count == 1
    if dry_run:
        assert not saved and not out.applied and not sp.calls
        assert (hub.tables, sp.liked) == before
    else:
        assert hub.tables["songs"][A]["album"] == "Release"
        assert saved["raw/spotify-pull/dummy.json.gz"]["liked"] == before[1]


@pytest.mark.parametrize("writes", [False, True])
def test_metadata_retry_keeps_original_archive_reference_and_observation_time(
    settings, mocker, writes
):
    from tests.test_release_regressions import Store, Spotify

    hub, sp = Store(member=False), Spotify(items=[])
    hub.tables["songs"].clear()
    hub.fail = "songs"
    objects, checkpoints = {}, []

    def put(settings, key, data):
        objects[key] = bytes(data)
        if key == run.archive.PENDING_KEY:
            checkpoints.append(json.loads(gzip.decompress(data)))

    mocker.patch("core.archive.put", side_effect=put)
    mocker.patch("core.archive.get", side_effect=lambda s, k: objects.get(k))
    mocker.patch("core.run.flags.file")
    first = datetime(2026, 9, 12, 12, tzinfo=timezone.utc)
    second = datetime(2026, 9, 12, 13, tzinfo=timezone.utc)
    out = run.reconcile(settings, spotify=sp, hub=hub, now=first, writes=writes)
    assert out.errors
    pending_bytes = objects[run.archive.PENDING_KEY]
    pending = json.loads(gzip.decompress(pending_bytes))
    direct = [
        r
        for op in pending["operations"]
        if op.get("table") == "provenance"
        for r in op["rows"]
        if r["rel"] == "evidence_of"
    ]
    assert direct and all(r["from_ref"] in objects for r in direct)
    assert all(r["detail"]["observed_at"] == "2026-09-12T12:00:00.000Z" for r in direct)
    save = run.archive.put

    def fail_raw(settings, key, data):
        if key.startswith("raw/"):
            raise RuntimeError("archive offline")
        save(settings, key, data)

    mocker.patch("core.archive.put", side_effect=fail_raw)
    with pytest.raises(RuntimeError, match="archive offline"):
        run.reconcile(settings, spotify=sp, hub=hub, now=second, writes=writes)
    assert objects[run.archive.PENDING_KEY] == pending_bytes
    mocker.patch("core.archive.put", save)
    assert not run.reconcile(settings, spotify=sp, hub=hub, now=second, writes=writes).errors
    assert all(hub.tables["provenance"][r["id"]] == r for r in direct)
    assert json.loads(gzip.decompress(objects[run.archive.PENDING_KEY])) is None
