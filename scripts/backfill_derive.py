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
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.hub import HubError  # noqa: E402
from core.metadata import require_observed_contract  # noqa: E402

SOURCES = {
    "deezer_genres": ("deezer_isrc", ("deezer_genres", "deezer_year")),
    "mb_tags": ("musicbrainz_isrc", ("mb_tags", "mb_first_year")),
    "first_year": ("first_year", ("first_year",)),
}
YEARS = ("album_year", "deezer_year", "mb_first_year")
COLS = list(
    dict.fromkeys(
        [
            "id",
            "hub_at",
            "deleted_at",
            *YEARS,
            *[c for _, fields in SOURCES.values() for c in fields],
        ]
    )
)
PROOF_COLS = ["id", "hub_at", "from_kind", "inputs_hash", "rel", "asserted_by", "deleted_at"]
RETRY_DELAYS = (5, 15)
OUTAGE_LIMIT = 5
BATCH_SIZE = 50
SOURCE_BATCH_SIZES = {"first_year": 20}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--table", default="songs")
    p.add_argument("--col", choices=SOURCES, help="one source group, then refresh first_year")
    p.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    p.add_argument(
        "--ids-file", type=Path, help="JSON array of explicit recording IDs (first_year only)"
    )
    p.add_argument(
        "--refresh", action="store_true", help="recompute selected years despite matching proofs"
    )
    p.add_argument(
        "--dry-run", action="store_true", help="read and report selection without deriving"
    )
    return p.parse_args(argv)


def complete(row: dict, table: str, col: str, proofs: dict) -> bool:
    name, fields = SOURCES[col]
    if col != "first_year" and not any(row.get(c) not in (None, "", [], "[]") for c in fields):
        return False
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
        field in row
        and (p := proofs.get(f"{table}:{row['id']}:{field}", {})).get("inputs_hash") == digest
        and p.get("from_kind") == f"http:{name}"
        for field in fields
    )


def run(
    hub,
    table: str,
    col: str | None,
    sleep=None,
    batch_size: int = BATCH_SIZE,
    *,
    ids: list[str] | None = None,
    refresh: bool = False,
    dry_run: bool = False,
) -> dict:
    sleep = sleep or time.sleep
    if not 1 <= batch_size <= BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {BATCH_SIZE}")
    groups = list(SOURCES) if col is None else list(dict.fromkeys([col, "first_year"]))
    if col is not None and col not in SOURCES:
        raise ValueError(f"unknown source column: {col}")
    if ids is not None:
        if (
            not isinstance(ids, list)
            or not ids
            or any(not isinstance(i, str) or not i.strip() or i != i.strip() for i in ids)
            or len(set(ids)) != len(ids)
        ):
            raise ValueError("ids-file must contain a nonempty JSON array of unique nonempty IDs")
        if col != "first_year":
            raise ValueError("bounded selection currently supports --col first_year only")
        ids = sorted(ids)
    if refresh and ids is None:
        raise ValueError("--refresh requires --ids-file; no unbounded refresh")
    require_observed_contract(hub)
    if refresh:
        properties = [p for p in hub.catalog()["properties"] if not p.get("deleted_at")]
        bound = [
            (p.get("tbl"), p.get("col"))
            for p in properties
            if p.get("derived_by") == "http:first_year"
        ]
        prop = next((p for p in properties if (p.get("tbl"), p.get("col")) == (table, col)), {})
        inputs = prop.get("inputs")
        if isinstance(inputs, str):
            inputs = json.loads(inputs)
        if bound != [(table, "first_year")] or inputs != list(YEARS):
            raise HubError(
                "refresh requires first_year to be the sole output with the expected year inputs"
            )
    proofs, current = {}, {}
    cursors = {"provenance": "", table: ""}

    def read_state(selected=None):
        if ids is not None:
            selected = ids if selected is None else selected
            requests = [
                (name, row_id, columns)
                for song_id in selected
                for name, row_id, columns in (
                    ("provenance", f"{table}:{song_id}:first_year", PROOF_COLS),
                    (table, song_id, [*COLS, "liked", "liked_at"]),
                )
            ]

            def pull(request):
                name, row_id, columns = request
                rows = hub.pull(name, columns, where={"id": row_id})
                if any(row.get("id") != row_id for row in rows):
                    raise HubError("bounded read returned an unrequested row")
                return rows

            # Only reads run concurrently; derivations remain bounded and sequential.
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(pull, requests))
            for (name, row_id, _), rows in zip(requests, results):
                state = proofs if name == "provenance" else current
                state.pop(row_id, None)
                for row in rows:
                    if not row.get("deleted_at") and (
                        name != "provenance"
                        or (row.get("rel") == "derived_from" and row.get("asserted_by") == "hub")
                    ):
                        state[row_id] = row
            return proofs, current
        # Proofs first: newer row inputs must invalidate older proofs. Commit
        # neither cache nor cursor until BOTH reads succeed. hub_at is inclusive.
        changes = [
            ("provenance", proofs, hub.pull("provenance", PROOF_COLS, cursors["provenance"] or "")),
            (table, current, hub.pull(table, COLS, cursors[table] or "")),
        ]
        for name, state, delta in changes:
            cursor = cursors[name]
            if not cursor:
                state.clear()
            for row in delta:
                if row.get("deleted_at") or (
                    name == "provenance"
                    and (row.get("rel") != "derived_from" or row.get("asserted_by") != "hub")
                ):
                    state.pop(row["id"], None)
                else:
                    state[row["id"]] = row
            # A legacy unstamped row can change without appearing in a delta.
            # Keep that table on full refreshes for the rest of this run.
            stamps = [row.get("hub_at") for row in delta]
            cursors[name] = (
                None
                if cursor is None or any(not isinstance(s, str) or not s for s in stamps)
                else max([cursor, *stamps])
            )
        return proofs, current

    proofs, current = read_state()
    if ids is not None and set(current) != set(ids):
        raise HubError(
            "selected IDs missing or deleted: " + ",".join(sorted(set(ids) - set(current)))
        )
    rows = sorted(current.values(), key=lambda r: r["id"])
    protected = (*YEARS, "liked", "liked_at")
    baseline = {r["id"]: dict(r) for r in rows}
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
        "dry_run": dry_run,
        "refresh": refresh,
        "selected_ids": [r["id"] for r in rows],
        "planned_ids": [],
    }
    done = {row["id"]: set() for row in rows}
    refresh_year = set()
    outages = dict.fromkeys(groups, 0)
    for source in groups:
        if source == "first_year":
            proofs, current = read_state()
        pending = []
        for row in rows:
            row_id = row["id"]
            if ids is not None and row_id not in current:
                raise HubError(f"selected ID disappeared before planning: {row_id}")
            row = current.get(row_id, {"id": row_id})
            needs_refresh = source == "first_year" and row_id in refresh_year
            if not refresh and not needs_refresh and complete(row, table, source, proofs):
                out["skipped"] += 1
                done[row_id].add(source)
                continue
            if source in out["stopped_sources"]:
                out["deferred"][source] = out["deferred"].get(source, 0) + 1
                continue
            pending.append(row_id)

        out["planned_ids"].extend(i for i in pending if i not in out["planned_ids"])
        if dry_run:
            continue
        source_batch_size = min(batch_size, SOURCE_BATCH_SIZES.get(source, batch_size))
        for start in range(0, len(pending), source_batch_size):
            batch = pending[start : start + source_batch_size]
            unresolved = list(batch)
            attempts_by_id = dict.fromkeys(batch, 0)
            last_errors = {}
            rate_limited = False
            for attempt, retry_delay in enumerate((0, *RETRY_DELAYS), 1):
                if retry_delay:
                    sleep(retry_delay)
                out["attempts"] += 1
                attempts_by_id.update(dict.fromkeys(unresolved, attempt))
                failed = {}
                try:
                    if ids is not None:
                        proofs, current = read_state(unresolved)
                        for row_id in unresolved:
                            before, latest = baseline[row_id], current.get(row_id, {})
                            if row_id not in current or (
                                refresh
                                and any(
                                    latest.get(c) != before.get(c)
                                    for c in (*protected, "first_year")
                                )
                            ):
                                failed[row_id] = {
                                    "id": row_id,
                                    "col": source,
                                    "error": "selection changed before derive; review a fresh preview",
                                }
                    requested = [i for i in unresolved if i not in failed]
                    result = (
                        hub.derive(table, requested, col=source) if requested else {"failed": []}
                    )
                    result_failures = result.get("failed", [])
                    for error in result_failures:
                        error_id = error.get("id")
                        if error_id in unresolved:
                            failed.setdefault(error_id, error)
                    if any(error.get("id") not in unresolved for error in result_failures):
                        for row_id in unresolved:
                            failed.setdefault(
                                row_id,
                                {
                                    "id": row_id,
                                    "col": source,
                                    "error": "hub returned a failure without a matching batch ID",
                                },
                            )
                    proofs, current = read_state(unresolved if ids is not None else None)
                    for row_id in unresolved:
                        if row_id not in failed and not (
                            row_id in current and complete(current[row_id], table, source, proofs)
                        ):
                            failed[row_id] = {
                                "id": row_id,
                                "col": source,
                                "error": "unresolved: source fields/proofs not confirmed",
                            }
                    if refresh:
                        for row_id in unresolved:
                            if row_id in failed:
                                continue
                            before, after = baseline[row_id], current.get(row_id, {})
                            expected = min(
                                (before[k] for k in YEARS if before.get(k) is not None),
                                default=None,
                            )
                            if any(before.get(c) != after.get(c) for c in protected):
                                failed[row_id] = {
                                    "id": row_id,
                                    "col": source,
                                    "error": "source inputs or likes changed during derive",
                                }
                            elif after.get("first_year") != expected:
                                failed[row_id] = {
                                    "id": row_id,
                                    "col": source,
                                    "error": "first_year inconsistent with selected source inputs",
                                }
                    successful = set(unresolved) - set(failed)
                    out["derived"] += len(successful)
                except HubError as exc:
                    for row_id in unresolved:
                        failed.setdefault(row_id, {"id": row_id, "col": source, "error": str(exc)})
                    successful = set()

                for row_id in successful:
                    done[row_id].add(source)
                    if source != "first_year":
                        refresh_year.add(row_id)
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
                if refresh:
                    # A repair with unexpected output needs inspection, not blind retries.
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
    if ids is not None:
        failed_ids = {e["id"] for e in out["failed"]}
        out["rows"] = [
            {
                "id": row_id,
                "before": baseline[row_id].get("first_year"),
                "expected": min(
                    (baseline[row_id][k] for k in YEARS if baseline[row_id].get(k) is not None),
                    default=None,
                ),
                "after": current.get(row_id, {}).get("first_year"),
                "status": "failed"
                if row_id in failed_ids
                else "complete"
                if done[row_id]
                else "planned"
                if dry_run
                else "deferred",
            }
            for row_id in ids
        ]
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
        ids = json.loads(args.ids_file.read_text()) if args.ids_file else None
        if args.ids_file and not isinstance(ids, list):
            raise ValueError("ids-file must contain a nonempty JSON array of unique nonempty IDs")
        out = run(
            hub,
            args.table,
            args.col,
            batch_size=args.batch_size,
            ids=ids,
            refresh=args.refresh,
            dry_run=args.dry_run,
        )
    except (HubError, KeyError, ValueError, OSError) as exc:
        print(json.dumps({"error": str(exc), "complete": False}), flush=True)
        return 1
    print(json.dumps(out), flush=True)
    return int(bool(out["failed"] or out["deferred"]))


if __name__ == "__main__":
    sys.exit(main())
