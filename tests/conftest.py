import pytest

from core.config import Settings


@pytest.fixture
def settings():
    return Settings(
        spotify_client_id="cid",
        spotify_client_secret="csec",
        spotify_refresh_token="rtok",
        life_hub_url="https://hub.test",
        life_hub_token="hubtok",
        notion_token="ntok",
        notion_tasks_data_source_id="ds",
        notion_project_page_id="proj",
        r2_account_id="acct",
        r2_bucket="bucket",
        r2_api_token="r2tok",
    )
