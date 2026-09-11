# /// script
# requires-python = ">=3.13"
# dependencies = ["httpx"]
# ///
"""Resume music metadata from hub provenance, batching up to 50 IDs per request.

Requires LIFE_HUB_URL and LIFE_HUB_TOKEN. No Spotify writes or local state.
"""

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from contextlib import closing
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
BATCH_SIZE = 50


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--table", default="songs")
    p.add_argument("--col", choices=SOURCES, help="one source group, then refresh first_year")
    p.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    return p.parse_args(argv)


def complete(row: dict, table: str, col: str, proofs: dict) -> bool:
    name, fields = SOURCES[col]
    if col == "title" and row.get("spotify_ids") in ([], "[]"):
        fields = ("spotify_ids", "spotify_playable")  # Spotify no-match omits title/year.
    values = [row.get(c) for c in YEARS] if col == "first_year" else [row["id"]]
    # D1 binds JS Number inputs as REAL even for catalog int years. Match
    # validate.js inputsHash's SQLite rendering; text and NULL stay unchanged.
    values = [float(v) if isinstance(v, (int, float)) else v for v in values]
    with closing(sqlite3.connect(":memory:")) as db:
        raw = db.execute(
            "SELECT json_array(" + ",".join("CAST(? AS TEXT)" for _ in values) + ")", values
        ).fetchone()[0]
    digest = hashlib.sha256(raw.encode()).hexdigest()
    return all(
        (p := proofs.get(f"{table}:{row['id']}:{field}", {})).get("inputs_hash") == digest
        and p.get("from_kind") == f"http:{name}"
        for field in fields
    )


def run(hub, table: str, col: str | None, sleep=None, batch_size: int = BATCH_SIZE) -> dict:
    sleep = sleep or time.sleep
    if not 1 <= batch_size <= BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {BATCH_SIZE}")
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
        "retry_at": {},
    }
    done = {row["id"]: set() for row in rows}
    refresh_year = set()
    outages = dict.fromkeys(groups, 0)
    for source in groups:
        pending = []
        for row in rows:
            row_id = row["id"]
            needs_refresh = source == "first_year" and row_id in refresh_year
            if not needs_refresh and complete(row, table, source, proofs):
                out["skipped"] += 1
                done[row_id].add(source)
                continue
            if source in out["stopped_sources"]:
                out["deferred"][source] = out["deferred"].get(source, 0) + 1
                continue
            if source != "first_year":
                refresh_year.add(row_id)
            pending.append(row_id)

        for start in range(0, len(pending), batch_size):
            batch = pending[start : start + batch_size]
            unresolved = list(batch)
            attempts_by_id = dict.fromkeys(batch, 0)
            last_errors = {}
            rate_limited = False
            for attempt, retry_delay in enumerate((0, *RETRY_DELAYS), 1):
                if retry_delay:
                    sleep(retry_delay)
                out["attempts"] += 1
                attempts_by_id.update(dict.fromkeys(unresolved, attempt))
                try:
                    result = hub.derive(table, unresolved, col=source)
                    failed = {}
                    result_failures = result.get("failed", [])
                    for error in result_failures:
                        error_id = error.get("id")
                        if error_id in unresolved:
                            failed.setdefault(error_id, error)
                    if any(error.get("id") not in unresolved for error in result_failures):
                        failed = {
                            row_id: {
                                "id": row_id,
                                "col": source,
                                "error": "hub returned a failure without a matching batch ID",
                            }
                            for row_id in unresolved
                        }
                    expected = len(unresolved) - len(failed)
                    if result.get("derived", 0) != expected:
                        failed = {
                            row_id: {
                                "id": row_id,
                                "col": source,
                                "error": "expected one confirmed source write per batch ID",
                            }
                            for row_id in unresolved
                        }
                        successful = set()
                    else:
                        successful = set(unresolved) - set(failed)
                        out["derived"] += result["derived"]
                except HubError as exc:
                    failed = {
                        row_id: {"id": row_id, "col": source, "error": str(exc)}
                        for row_id in unresolved
                    }
                    successful = set()

                for row_id in successful:
                    done[row_id].add(source)
                if not failed:
                    unresolved = []
                    last_errors = {}
                    break

                last_errors = failed
                cooldowns = []
                for error in failed.values():
                    delay = error.get("retry_after")
                    valid_delay = type(delay) is int and delay >= 0
                    if error.get("status") == 429 or (error.get("status") == 503 and valid_delay):
                        cooldowns.append(max(1, delay) if valid_delay else 60)
                if cooldowns:
                    out["retry_at"][source] = time.time() + max(cooldowns)
                    if source not in out["stopped_sources"]:
                        out["stopped_sources"].append(source)
                    rate_limited = True
                    unresolved = list(failed)
                    break
                print(
                    f"backfill_derive: {source} batch {batch[0]}-{batch[-1]} attempt {attempt}: "
                    f"{json.dumps(list(failed.values()))}",
                    flush=True,
                )
                unresolved = list(failed)

            if unresolved:
                out["failed"].extend(
                    {
                        "id": row_id,
                        "col": source,
                        "attempts": attempts_by_id[row_id],
                        "errors": [last_errors[row_id]],
                    }
                    for row_id in unresolved
                )
                if rate_limited:
                    out["deferred"][source] = (
                        out["deferred"].get(source, 0) + len(pending) - start - len(batch)
                    )
                    break
                # Record-specific HTTP failures continue; a whole batch of
                # transport failures pauses the source after a short circuit.
                unavailable = len(unresolved) == len(batch) and all(
                    any(word in str(error).lower() for word in ("timeout", "unreachable"))
                    for error in last_errors.values()
                )
                outages[source] = outages[source] + len(unresolved) if unavailable else 0
                if outages[source] >= OUTAGE_LIMIT:
                    if source not in out["stopped_sources"]:
                        out["stopped_sources"].append(source)
                    remaining = len(pending) - start - len(batch)
                    out["deferred"][source] = out["deferred"].get(source, 0) + remaining
                    print(
                        f"backfill_derive: pausing {source} after {OUTAGE_LIMIT} consecutive "
                        "recordings exhausted timeout/transport retries; other sources continue",
                        flush=True,
                    )
                    break
            else:
                outages[source] = 0

    out["completed_recordings"] = sum(
        all(source in done[row_id] for source in groups) for row_id in done
    )
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
        out = run(hub, args.table, args.col, batch_size=args.batch_size)
    except (HubError, KeyError) as exc:
        print(json.dumps({"error": str(exc), "complete": False}), flush=True)
        return 1
    print(json.dumps(out), flush=True)
    return int(bool(out["failed"] or out["deferred"]))


if __name__ == "__main__":
    sys.exit(main())
