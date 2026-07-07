# /// script
# requires-python = ">=3.13"
# dependencies = ["pydantic>=2.10", "structlog>=25.1"]
# ///
"""Normalize the Spotify GDPR export zip into data/snapshot.json (gitignored).

Doubles as the safety backup of Spotify data. Run: uv run scripts/ingest_export.py
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

from core.export_ingest import load_followed_artists, load_playlists  # noqa: E402

ZIP_PATH = ROOT / "data" / "raw" / "my_spotify_data (3).zip"
SNAPSHOT_PATH = ROOT / "data" / "snapshot.json"


def main() -> None:
    playlists = load_playlists(ZIP_PATH)
    artists = load_followed_artists(ZIP_PATH)
    snapshot = {
        "source": ZIP_PATH.name,
        "playlists": [p.model_dump(mode="json") for p in playlists],
        "followed_artists": [a.model_dump(mode="json") for a in artists],
    }
    SNAPSHOT_PATH.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False))
    n_songs = sum(len(p.songs) for p in playlists)
    print(f"Wrote {SNAPSHOT_PATH}")
    print(f"  playlists: {len(playlists)}")
    print(f"  songs: {n_songs}")
    print(f"  followed artists: {len(artists)}")


if __name__ == "__main__":
    main()
