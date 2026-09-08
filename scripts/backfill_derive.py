# /// script
# requires-python = ">=3.13"
# dependencies = ["httpx", "pydantic-settings", "structlog"]
# ///
"""Backfill derived columns (title, deezer_genres, mb_tags, first_year) for
rows that predate the derivation service, in chunks of 50 with a pause
between chunks so each hub /v1/derive call (which paces MusicBrainz itself)
stays inside its own time budget.

    op run --env-file=.env.tpl -- uv run scripts/backfill_derive.py [--table songs] [--col first_year]
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

DERIVED_COLS = ["title", "deezer_genres", "mb_tags", "first_year"]
COLS = ["id", *DERIVED_COLS, "deleted_at"]
CHUNK = 50
SLEEP_S = 60


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--table", default="songs")
    p.add_argument(
        "--col",
        default=None,
        help="narrow the null-check to one derived column (default: any of "
        + ", ".join(DERIVED_COLS),
    )
    return p.parse_args(argv)


def select_ids(rows: list[dict], col: str | None = None) -> list[str]:
    """Ids of non-deleted rows where `col` (or, by default, any derived column) is still null."""
    cols = [col] if col else DERIVED_COLS
    return [
        r["id"]
        for r in rows
        if not r.get("deleted_at") and any(r.get(c) in (None, "") for c in cols)
    ]


def run(hub, table: str, col: str | None, sleep=time.sleep) -> dict:
    ids = select_ids(hub.pull(table, COLS), col)
    derived, failed = 0, []
    for i in range(0, len(ids), CHUNK):
        out = hub.derive(table, ids[i : i + CHUNK])
        derived += out["derived"]
        failed += out["failed"]
        print(f"backfill_derive: {derived}/{len(ids)} derived, {len(failed)} failed")
        if i + CHUNK < len(ids):
            sleep(SLEEP_S)
    return {"derived": derived, "failed": failed}


def main() -> None:
    from core.config import Settings
    from core.hub import Hub

    args = parse_args()
    settings = Settings()
    hub = Hub(settings.life_hub_url, settings.life_hub_token)
    out = run(hub, args.table, args.col)
    if out["failed"]:
        print(f"backfill_derive: {len(out['failed'])} failed: {out['failed']}")
        sys.exit(1)


if __name__ == "__main__":
    main()
