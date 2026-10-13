"""Configure playlist kinds and smart rules through the deployed worker (operator tool).

    uv run scripts/rules.py list
    uv run scripts/rules.py create "<name>" '<rule json>'     # new private smart playlist
    uv run scripts/rules.py smart <playlist_id> '<rule json>'  # convert an owned playlist
    uv run scripts/rules.py curated <playlist_id>              # classify a playlist as curated
    uv run scripts/rules.py archive <playlist_id>              # preserve a read-only backup
    uv run scripts/rules.py clear <playlist_id>                # smart -> curated
    uv run scripts/rules.py cover <playlist_id> <image.jpg>    # set the cover (JPEG <= ~192 KB)
    uv run scripts/rules.py import-covers [--apply]            # seed covers from Spotify's images

Rules are JSON v1 (see src/core/rules.py) and reference other playlists by stable ID
(`in_playlist_ids_any`, `not_in_playlist_ids`) and reuse another smart playlist's rule
(`matches_rule_ids_any`, `not_matches_rule_ids`). Runs in the serialized worker with
the operator's Modal auth; the worker writes the playlists row itself. A cover is
retained in soma and uploaded to Spotify by the next reconcile; `import-covers` lists
(or with --apply retains) each owned playlist's current custom Spotify image.
"""

import base64
import json
import sys
from pathlib import Path

import modal

APP_NAME = "music-sync"


def main(argv: list[str]) -> int:
    match [a for a in argv if a]:
        case ["list"]:
            body = {"action": "list"}
        case ["create", name, rule]:
            body = {"action": "create", "name": name, "rule": json.loads(rule)}
        case ["smart", pid, rule]:
            body = {"action": "smart", "playlist_id": pid, "rule": json.loads(rule)}
        case [("curated" | "clear" | "archive") as action, pid]:
            body = {"action": action, "playlist_id": pid}
        case ["cover", pid, path]:
            image = base64.b64encode(Path(path).read_bytes()).decode()
            body = {"action": "cover", "playlist_id": pid, "image_b64": image}
        case ["import-covers", *flags] if set(flags) <= {"--apply"}:
            body = {"action": "import_covers", "apply": "--apply" in flags}
        case _:
            print(__doc__, file=sys.stderr)
            return 2
    result = modal.Function.from_name(APP_NAME, "worker").remote("rules", body)
    body = getattr(result, "body", None)
    print(body.decode() if isinstance(body, bytes) else json.dumps(result, indent=2))
    return 1 if body is not None else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
