"""One-off: found_at for songs you brought into the catalog yourself before
Music Sync kept it (core/new_songs.found_from_history). Dry run by default.

    op run --env-file=.env.tpl -- uv run python scripts/backfill_found_at.py [--apply]
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core import mirror, new_songs  # noqa: E402
from core.config import Settings  # noqa: E402
from core.hub import Hub, with_read_retries  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    settings = Settings()
    hub = with_read_retries(Hub(settings.soma_hub_url, settings.soma_hub_token))
    m = mirror.load_mirror(hub)
    edges = hub.pull(
        "provenance",
        ["id", "from_kind", "from_ref", "to_ref", "detail", "deleted_at"],
        where={"to_kind": "songs", "rel": "imported_from", "from_kind": "playlist"},
    )
    edges = [e for e in edges if not e.get("deleted_at")]
    memberships = [*m.memberships.values(), *m.deleted_memberships]
    found = new_songs.found_from_history(m.songs, m.playlists, memberships, edges)
    print(f"{len(found)} songs get found_at")
    for isrc, at in sorted(found.items(), key=lambda kv: kv[1], reverse=True)[:10]:
        print(" ", at, isrc, m.songs[isrc].title)
    if args.apply and found:
        hub.push("songs", [{"id": isrc, "found_at": at} for isrc, at in sorted(found.items())])
        print("applied")


if __name__ == "__main__":
    main()
