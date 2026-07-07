# /// script
# requires-python = ">=3.13"
# dependencies = ["httpx>=0.28", "pydantic>=2.10", "pydantic-settings>=2.7", "structlog>=25.1"]
# ///
"""Sync followed artists from the GDPR export into the sandbox Notion DB.

First run creates the Followed Artists DB under the sandbox page and records
its ids in sandbox_config.json. Reruns are idempotent (upsert on Spotify ID).
When Spotify developer creds exist in the env, live API data (genres,
followers) enriches the same rows — matched on Spotify ID.

Run: op run --env-file=.env.tpl -- uv run scripts/sync_followed_to_notion.py
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

from core.config import Settings  # noqa: E402
from core.export_ingest import load_followed_artists  # noqa: E402
from core.notion_sync import (  # noqa: E402
    NotionClient,
    create_followed_artists_db,
    sync_followed_artists,
)
from core.spotify_client import SpotifyClient, merge_followed_artists  # noqa: E402

CONFIG_PATH = ROOT / "sandbox_config.json"
ZIP_PATH = ROOT / "data" / "raw" / "my_spotify_data (3).zip"


def main() -> None:
    settings = Settings()
    client = NotionClient(token=settings.notion_token)

    cfg = json.loads(CONFIG_PATH.read_text())
    if "followed_artists_data_source_id" not in cfg:
        cfg.update(create_followed_artists_db(client, cfg["sandbox_page_id"]))
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")
        print(f"Created Followed Artists DB, updated {CONFIG_PATH}")

    artists = load_followed_artists(ZIP_PATH)
    if settings.spotify_refresh_token:
        live = SpotifyClient(settings).get_followed_artists()
        artists = merge_followed_artists(artists, live)
        print(f"Enriched with {len(live)} live artists")
    else:
        print("No Spotify creds — export data only (genres/followers stay empty)")

    stats = sync_followed_artists(client, cfg, artists)
    print(json.dumps(stats))


if __name__ == "__main__":
    main()
