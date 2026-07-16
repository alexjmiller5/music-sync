# /// script
# requires-python = ">=3.13"
# dependencies = ["httpx>=0.28", "pydantic>=2.10", "pydantic-settings>=2.7", "structlog>=25.1"]
# ///
"""Sync data/snapshot.json into the sandbox Notion Playlists/Songs DBs.

First run creates the sandbox page + DBs under the NOTION_PARENT_PAGE_ID page
and writes sandbox_config.json (gitignored, per-user). Reruns are idempotent.

Run: op run --env-file=.env.tpl -- uv run scripts/sync_playlists_to_notion.py
(or with NOTION_TOKEN already in the env)
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

from core.config import Settings  # noqa: E402
from core.models import Playlist  # noqa: E402
from core.notion_sync import NotionClient, create_sandbox, sync_snapshot  # noqa: E402

CONFIG_PATH = ROOT / "sandbox_config.json"
SNAPSHOT_PATH = ROOT / "data" / "snapshot.json"


def main() -> None:
    settings = Settings()
    client = NotionClient(token=settings.notion_token)

    if CONFIG_PATH.exists():
        cfg = json.loads(CONFIG_PATH.read_text())
    else:
        cfg = create_sandbox(client, settings.notion_parent_page_id)
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")
        print(f"Created sandbox + DBs, wrote {CONFIG_PATH}")

    snapshot = json.loads(SNAPSHOT_PATH.read_text())
    playlists = [Playlist(**p) for p in snapshot["playlists"]]
    stats = sync_snapshot(client, cfg, playlists)
    print(json.dumps(stats))


if __name__ == "__main__":
    main()
