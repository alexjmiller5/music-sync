import json
import stat

import httpx
import pytest


def test_export_retains_complete_source_privately_without_inventing_follow_dates(
    tmp_path, monkeypatch, settings
):
    from scripts import followed_artists

    artists = [{"id": "artist-one", "name": "Artist", "external_urls": {"spotify": "original"}}]

    class Source:
        def __init__(self, config):
            assert config is settings

        def me(self):
            return {"id": "account"}

        def get_followed_artists(self):
            return artists

    monkeypatch.setattr(followed_artists, "Settings", lambda: settings)
    monkeypatch.setattr(followed_artists, "SpotifyClient", Source)
    path = tmp_path / "followed.json"
    assert followed_artists.main(["--output", str(path)]) == 0
    receipt = json.loads(path.read_text())
    assert receipt["artists"] == artists and receipt["complete"] is True
    assert receipt["account_id"] == "account" and receipt["follow_dates_known"] is False
    assert receipt["observed_at"] and receipt["completed_at"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        followed_artists.main(["--output", str(path)])


def test_denied_existing_scope_produces_incomplete_receipt_without_reenrollment(
    tmp_path, monkeypatch, settings, capsys
):
    from scripts import followed_artists

    class Source:
        def __init__(self, config):
            pass

        def me(self):
            return {"id": "account"}

        def get_followed_artists(self):
            httpx.Response(
                403, text="private body", request=httpx.Request("GET", "https://source.test")
            ).raise_for_status()

    monkeypatch.setattr(followed_artists, "Settings", lambda: settings)
    monkeypatch.setattr(followed_artists, "SpotifyClient", Source)
    path = tmp_path / "denied.json"
    assert followed_artists.main(["--output", str(path)]) == 1
    receipt = json.loads(path.read_text())
    assert receipt["complete"] is False and receipt["http_status"] == 403
    assert "artists" not in receipt
    assert "private body" not in path.read_text() + capsys.readouterr().out
