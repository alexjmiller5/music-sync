# /// script
# requires-python = ">=3.13"
# dependencies = ["httpx"]
# ///
"""Resume music metadata from hub provenance, one recording/source per request.

Requires LIFE_HUB_URL and LIFE_HUB_TOKEN. No Spotify writes or local state.
"""

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.hub import HubError  # noqa: E402

SOURCES = {
    "title": (
        "spotify_isrc",
        (
            "spotify_ids",
            "spotify_playable",
            "title",
            "artists",
            "album",
            "album_year",
            "duration_ms",
        ),
    ),
    "deezer_genres": ("deezer_isrc", ("deezer_genres", "deezer_year")),
    "mb_tags": ("musicbrainz_isrc", ("mb_tags", "mb_first_year")),
    "first_year": ("first_year", ("first_year",)),
}
YEARS = ("album_year", "deezer_year", "mb_first_year")
COLS = ["id", "deleted_at", *[c for _, fields in SOURCES.values() for c in fields]]
PROOF_COLS = ["id", "from_kind", "inputs_hash", "rel", "asserted_by", "deleted_at"]
RETRY_DELAYS = (5, 15)
OUTAGE_LIMIT = 5


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--table", default="songs")
    p.add_argument("--col", choices=SOURCES, help="one source group, then refresh first_year")
    return p.parse_args(argv)


def complete(row: dict, table: str, col: str, proofs: dict) -> bool:
    name, fields = SOURCES[col]
    if col == "title" and row.get("spotify_ids") in ([], "[]"):
        fields = ("spotify_ids", "spotify_playable")  # Spotify no-match omits title/year.
    values = [row.get(c) for c in YEARS] if col == "first_year" else [row["id"]]
    # These inputs are ISRC text and catalog int years, cast to text by the hub.
    raw = json.dumps(
        [None if v is None else str(v) for v in values], separators=(",", ":"), ensure_ascii=False
    )
    digest = hashlib.sha256(raw.encode()).hexdigest()
    return all(
        (p := proofs.get(f"{table}:{row['id']}:{field}", {})).get("inputs_hash") == digest
        and p.get("from_kind") == f"http:{name}"
        for field in fields
    )


def run(hub, table: str, col: str | None, sleep=None) -> dict:
    sleep = sleep or time.sleep
    groups = list(SOURCES) if col is None else list(dict.fromkeys([col, "first_year"]))
    if col is not None and col not in SOURCES:
        raise ValueError(f"unknown source column: {col}")
    # Read proofs before rows, so newly observed input changes invalidate older proofs.
    proofs = {
        p["id"]: p
        for p in hub.pull("provenance", PROOF_COLS)
        if not p.get("deleted_at")
        and p.get("rel") == "derived_from"
        and p.get("asserted_by") == "hub"
    }
    rows = sorted(
        (r for r in hub.pull(table, COLS) if not r.get("deleted_at")), key=lambda r: r["id"]
    )
    out = {
        "total_recordings": len(rows),
        "completed_recordings": 0,
        "total_sources": len(rows) * len(groups),
        "derived": 0,
        "skipped": 0,
        "attempts": 0,
        "failed": [],
        "stopped_sources": [],
        "deferred": {},
    }
    outages = dict.fromkeys(groups, 0)
    for row in rows:
        row_ok, changed = True, False
        for source in groups:
            if not (source == "first_year" and changed) and complete(row, table, source, proofs):
                out["skipped"] += 1
                continue
            if source in out["stopped_sources"]:
                out["deferred"][source] = out["deferred"].get(source, 0) + 1
                row_ok = False
                continue
            # Even a failed response can follow a committed source-year update.
            changed = changed or source != "first_year"
            errors = []
            for attempt, delay in enumerate((0, *RETRY_DELAYS), 1):
                if delay:
                    sleep(delay)
                out["attempts"] += 1
                try:
                    result = hub.derive(table, [row["id"]], col=source)
                    errors = result["failed"]
                    if not errors and result["derived"] != 1:
                        errors = [{"error": "expected one source write; none confirmed"}]
                except HubError as exc:
                    errors = [{"error": str(exc)}]
                if not errors:
                    break
                print(
                    f"backfill_derive: {row['id']} {source} attempt {attempt}: "
                    f"{json.dumps(errors)}",
                    flush=True,
                )
            if errors:
                row_ok = False
                out["failed"].append(
                    {"id": row["id"], "col": source, "attempts": attempt, "errors": errors}
                )
                # Record-specific HTTP failures (including 502) must not stop the source.
                unavailable = all(
                    any(word in str(e).lower() for word in ("timeout", "unreachable"))
                    for e in errors
                )
                outages[source] = outages[source] + 1 if unavailable else 0
                if outages[source] >= OUTAGE_LIMIT:
                    out["stopped_sources"].append(source)
                    print(
                        f"backfill_derive: pausing {source} after {OUTAGE_LIMIT} consecutive "
                        "recordings exhausted timeout/transport retries; other sources continue",
                        flush=True,
                    )
            else:
                out["derived"] += 1
                outages[source] = 0
        out["completed_recordings"] += int(row_ok)
        print(
            f"backfill_derive: recordings {out['completed_recordings']}/{len(rows)}, "
            f"sources {out['derived'] + out['skipped']}/{out['total_sources']} "
            f"({out['derived']} derived, {out['skipped']} reused), "
            f"{len(out['failed'])} failed, {sum(out['deferred'].values())} deferred",
            flush=True,
        )
    return out


def main() -> int:
    from core.hub import Hub

    args = parse_args()
    try:
        hub = Hub(os.environ["LIFE_HUB_URL"], os.environ["LIFE_HUB_TOKEN"])
        out = run(hub, args.table, args.col)
    except (HubError, KeyError) as exc:
        print(json.dumps({"error": str(exc), "complete": False}), flush=True)
        return 1
    print(json.dumps(out), flush=True)
    return int(bool(out["failed"] or out["deferred"]))


if __name__ == "__main__":
    sys.exit(main())
