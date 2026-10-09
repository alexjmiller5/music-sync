"""Settings from env vars - Modal Secret in the cloud, `op run` locally.

One field per line in .env.tpl. Instantiate Settings() inside functions,
never at import time, so tests run without secrets.
"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    spotify_client_id: str
    spotify_client_secret: str
    spotify_refresh_token: str
    spotify_market: str = "US"
    soma_hub_url: str
    soma_hub_token: str
    notion_token: str = ""
    notion_tasks_data_source_id: str = ""
    notion_project_page_id: str = ""
    r2_account_id: str
    r2_bucket: str
    r2_api_token: str
    r2_access_key_id: str
    # Fernet key that encrypts workspace secrets at rest (core/workspaces.py).
    workspace_secret_key: str = ""
    inbox_cap: int = 100
    undo_days: int = 7
    # Which workspace these settings act for (core/workspaces.py); set by
    # workspaces.settings_for, never by env.
    workspace: str = "default"
    flags_task_config: dict | None = None
