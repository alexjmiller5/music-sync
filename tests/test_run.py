from datetime import datetime, timezone

from core import run
from core.spotify_client import SpotifyAuthError


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
    assert filed.call_args.args[2] == [] and "invalid_grant" in filed.call_args.args[3][0]
