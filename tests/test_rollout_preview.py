import copy

from core import curation
from tests.test_curation_policy import archive_store as archive_store
from tests.test_release_regressions import A, Store, Spotify, execute, raw


def test_bootstrap_manifest_uses_exact_curated_observation_and_excludes_exceptions():
    members = {("P", "A"), ("P", "B"), ("Q", "A")}
    rows = curation.bootstrap_candidates(set(), members, {"A": {"reason": "unliked_while_curated"}})
    assert rows == [{"isrc": "B", "source_playlist_ids": ["P"]}]


def test_full_dry_run_keeps_input_snapshot_and_migration_candidates(settings, archive_store):
    hub, sp = Store(liked=0), Spotify(liked=False)
    before = copy.deepcopy(hub.tables)
    out = execute(settings, sp, hub, dry_run=True)
    assert out.snapshot["spotify"]["items"]["P"] == [raw()]
    assert out.snapshot["spotify"]["liked"] == []
    assert out.snapshot["migration_candidates"] == [{"isrc": A, "source_playlist_ids": ["P"]}]
    assert out.snapshot["retained_remotely"] is False
    assert out.snapshot["mirror"]["songs"][0]["id"] == A
    assert hub.tables == before and sp.calls == [] and archive_store == {}


def test_preview_command_is_read_only_and_writes_private_receipt(tmp_path, monkeypatch, settings):
    import json
    from scripts import preview
    from core.actions import RunLog

    calls = []
    monkeypatch.setattr(preview, "Settings", lambda: settings)

    def reconcile(actual, *, dry_run):
        calls.append((actual, dry_run))
        return RunLog(dry_run=True, snapshot={"spotify": {"liked": []}})

    monkeypatch.setattr(preview.run, "reconcile", reconcile)
    output = tmp_path / "receipt.json"
    assert preview.main(["--output", str(output)]) == 0
    assert calls == [(settings, True)]
    assert json.loads(output.read_text())["snapshot"]["spotify"] == {"liked": []}
    assert output.stat().st_mode & 0o777 == 0o600
    try:
        preview.main(["--output", str(output)])
    except FileExistsError:
        pass
    else:
        raise AssertionError("must not overwrite retained evidence")
    assert len(calls) == 1


def test_preview_preserves_hub_revisions_for_later_concurrency_check(settings, archive_store):
    hub, sp = Store(), Spotify()
    hub.tables["songs"][A].update(
        updated_at="2026-09-08T00:00:00.000Z", hub_at="2026-09-08T00:00:01.000Z"
    )
    out = execute(settings, sp, hub, dry_run=True)
    assert out.snapshot["revisions"]["songs"][A] == {
        "updated_at": "2026-09-08T00:00:00.000Z",
        "hub_at": "2026-09-08T00:00:01.000Z",
    }


def test_preview_errors_are_safe_and_receipt_remains_valid_json(
    tmp_path, monkeypatch, settings, capsys
):
    import json
    from scripts import preview
    from core.hub import HubError

    monkeypatch.setattr(preview, "Settings", lambda: settings)

    def fail(*args, **kwargs):
        raise HubError("private response body")

    monkeypatch.setattr(preview.run, "reconcile", fail)
    path = tmp_path / "failed.json"
    assert preview.main(["--output", str(path)]) == 1
    assert json.loads(path.read_text())["incomplete"] is True
    captured = capsys.readouterr()
    assert "private response body" not in captured.out + captured.err


def test_preview_carries_prior_decision_evidence(settings, archive_store):
    hub, sp = Store(), Spotify()
    assert not execute(settings, sp, hub).errors
    out = execute(settings, sp, hub, dry_run=True)
    assert out.snapshot["curation_before"]["baseline"]["liked"] == [A]
    assert out.snapshot["mirror"]["observations"]
    assert "captures" in out.snapshot["mirror"]
    assert out.snapshot["planner_settings"]["inbox_cap"] == settings.inbox_cap
