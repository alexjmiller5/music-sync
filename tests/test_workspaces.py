"""Workspaces: whose Spotify, catalog hub and Notion a capture or reconcile acts for."""

import gzip
import json

import pytest

from core import archive, workspaces


@pytest.fixture
def objects(monkeypatch):
    stored = {}
    monkeypatch.setattr(workspaces.archive, "get", lambda settings, key: stored.get(key))
    monkeypatch.setattr(
        workspaces.archive, "put", lambda settings, key, value: stored.__setitem__(key, value)
    )
    return stored


def test_default_workspace_is_the_env_configuration(settings, objects):
    s = workspaces.settings_for(settings, "default")
    assert s.workspace == "default"
    assert s.spotify_refresh_token == settings.spotify_refresh_token


def test_a_workspace_overrides_only_the_per_user_fields(settings, objects):
    workspaces.save(
        settings,
        "friend",
        spotify_refresh_token="friend-rt",
        life_hub_url="https://friend.hub",
        inbox_cap="50",
    )
    s = workspaces.settings_for(settings, "friend")
    assert (s.workspace, s.spotify_refresh_token, s.life_hub_url, s.inbox_cap) == (
        "friend",
        "friend-rt",
        "https://friend.hub",
        50,
    )
    assert s.spotify_client_id == settings.spotify_client_id  # the app's own Spotify app
    assert s.r2_bucket == settings.r2_bucket  # the app's own state bucket


def test_app_fields_and_unknown_names_are_refused(settings, objects):
    with pytest.raises(workspaces.InvalidWorkspace, match="spotify_client_secret"):
        workspaces.save(settings, "friend", spotify_client_secret="x")
    with pytest.raises(workspaces.InvalidWorkspace):
        workspaces.save(settings, "Bad Id", notion_token="x")


def test_unknown_workspace_is_an_error(settings, objects):
    with pytest.raises(workspaces.UnknownWorkspace):
        workspaces.settings_for(settings, "ghost")


def test_ids_and_summary_never_show_secret_values(settings, objects):
    workspaces.save(settings, "friend", spotify_refresh_token="secret-rt", spotify_market="GB")
    assert workspaces.ids(settings) == ["default", "friend"]
    summary = workspaces.summary(settings, "friend")
    assert "secret-rt" not in json.dumps(summary)
    assert summary["set"] == ["spotify_market", "spotify_refresh_token"]
    assert summary["spotify_market"] == "GB"


def test_reconcile_gate(settings, objects, monkeypatch):
    monkeypatch.setenv("RECONCILE_ENABLED", "1")
    assert workspaces.reconcile_enabled(workspaces.settings_for(settings, "default"))
    workspaces.save(settings, "friend", notion_token="n")
    assert not workspaces.reconcile_enabled(workspaces.settings_for(settings, "friend"))
    workspaces.save(settings, "friend", reconcile_enabled="true")
    assert workspaces.reconcile_enabled(workspaces.settings_for(settings, "friend"))


def test_pending_reconcile_state_is_per_workspace(settings, objects):
    assert archive.pending_key(workspaces.settings_for(settings, "default")) == archive.PENDING_KEY
    workspaces.save(settings, "friend", notion_token="n")
    assert archive.pending_key(workspaces.settings_for(settings, "friend")) == (
        "music-sync/workspaces/friend/pending-reconcile.json.gz"
    )


def test_registry_is_one_gzipped_json_object(settings, objects):
    workspaces.save(settings, "friend", notion_token="n")
    assert json.loads(gzip.decompress(objects[workspaces.REGISTRY_KEY])) == {
        "friend": {"notion_token": "n"}
    }
