from core.config import Settings


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("NOTION_TOKEN", "ntn_test")
    monkeypatch.setenv("NOTION_PARENT_PAGE_ID", "page-id")
    s = Settings()
    assert s.notion_token == "ntn_test"
    assert s.notion_parent_page_id == "page-id"
    assert s.spotify_client_id is None  # Spotify creds optional until app exists
