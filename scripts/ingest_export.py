# /// script
# requires-python = ">=3.13"
# dependencies = ["pydantic>=2.10", "structlog>=25.1"]
# ///
"""Normalize the Spotify GDPR export zip into data/snapshot.json (gitignored).

Doubles as the safety backup of Spotify data.

Run: uv run scripts/ingest_export.py [export.zip]
(defaults to the newest zip in data/raw/)
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

from core.export_ingest import find_export_zip, load_followed_artists, load_playlists  # noqa: E402

SNAPSHOT_PATH = ROOT / "data" / "snapshot.json"


def main() -> None:
    try:
        zip_path = (
            Path(sys.argv[1]) if len(sys.argv) > 1 else find_export_zip(ROOT / "data" / "raw")
        )
    except FileNotFoundError as e:
        sys.exit(str(e))
    playlists = load_playlists(zip_path)
    artists = load_followed_artists(zip_path)
    snapshot = {
        "source": zip_path.name,
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
