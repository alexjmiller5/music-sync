from datetime import datetime, timezone

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
