"""Apply a plan in ordered batches, stopping at the first failed checkpoint."""

import hashlib
import json
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

import httpx
import structlog

from core import covers, new_songs
from core.model import Action

log = structlog.get_logger()
CHUNK = 500  # hub rows per checkpointed operation
# An arrangement replaces its playlist when its edits take more requests than this and than
# rewriting it (one request per 100 items).
REPLACE_AFTER = 10

HUB_TABLE = {
    "upsert_song": "songs",
    "upsert_playlist": "playlists",
    "upsert_membership": "playlist_songs",
    "delete_membership": "playlist_songs",
    "edge": "provenance",
    "insert_edge": "provenance",
}
SPOTIFY_WRITE_KINDS = {
    "like",
    "add_item",
    "readd_item",
    "remove_item",
    "arrange_items",
    "set_description",
    "set_cover",
    "delete_playlist",
}


@dataclass
class RunLog:
    applied: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    skipped: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    dry_run: bool = False
    errors: list[str] = field(default_factory=list)
    planned: list[dict] = field(default_factory=list)
    review_items: list[dict] = field(default_factory=list)
    snapshot: dict | None = None

    def summary(self) -> str:
        lines = [f"applied {k}: {v}" for k, v in sorted(self.applied.items()) if v]
        for category, predicate in (
            ("Planned Spotify mutations", lambda a: a["kind"] in SPOTIFY_WRITE_KINDS),
            ("Planned mirror patches", lambda a: a["kind"] in HUB_TABLE),
        ):
            details = [a for a in self.planned if predicate(a)]
            lines.append(f"{category}: {len(details)}")
            lines.extend(json.dumps(a, ensure_ascii=False, sort_keys=True) for a in details)
        lines.extend("review: " + json.dumps(item, sort_keys=True) for item in self.review_items)
        lines += [f"flag: {f}" for f in self.flags] + [f"error: {e}" for e in self.errors]
        return ("DRY RUN\n" if self.dry_run else "") + "\n".join(lines)


def _batches(actions: list[Action], writes: bool) -> list[dict]:
    ops = []
    # An arrangement planned from an observed order places its playlist's adds and
    # removes itself (core/reconcile.py smart materialization).
    arranged = {
        a.playlist_id for a in actions if a.kind == "arrange_items" and a.current is not None
    }
    for kind in (
        "like",
        "add_item",
        "remove_item",
        "readd_item",
        "arrange_items",
        "set_description",
        "set_cover",
        "delete_playlist",
    ):
        groups = defaultdict(list)
        for a in actions:
            if writes and a.kind == kind:
                groups[a.playlist_id].append(a)
        for pid, group in groups.items():
            if kind == "arrange_items":
                op = {"kind": kind, "playlist_id": pid, "uris": group[-1].uris, "count": 1}
                if group[-1].current is not None:
                    op["current"] = group[-1].current
                ops.append(op)
                continue
            if kind in ("add_item", "remove_item") and pid in arranged:
                continue
            if kind == "set_cover":
                ops.append({"kind": kind, "playlist_id": pid, **group[-1].row, "count": 1})
                continue
            ops.append(
                {
                    "kind": kind,
                    "playlist_id": pid,
                    "uris": list(dict.fromkeys(a.uri for a in group if a.uri)),
                    "text": group[-1].text,
                    "count": len(group),
                }
            )
    for table in ("songs", "playlists", "playlist_songs", "provenance"):
        rows, counts = {}, defaultdict(int)
        for a in actions:
            if a.kind != "insert_edge" and HUB_TABLE.get(a.kind) == table:
                rows.setdefault(a.row["id"], {}).update(a.row)
                counts[a.kind] += 1
        if rows:
            ops.append(
                {"kind": "hub", "table": table, "rows": list(rows.values()), "counts": dict(counts)}
            )
    evidence = [a.row for a in actions if a.kind == "insert_edge"]
    for index in range(0, len(evidence), 100):
        ops.append(
            {"kind": "hub_insert", "table": "provenance", "rows": evidence[index : index + 100]}
        )
    return ops


def apply(
    actions: list[Action],
    spotify,
    hub,
    dry_run: bool,
    writes: bool = True,
    *,
    checkpoint=None,
    pending: list[dict] | None = None,
    market: str | None = None,
    deadline: float | None = None,
) -> RunLog:
    """`deadline` (time.monotonic) stops cleanly between operations; the rest stays
    pending so rerunning the same operation resumes it."""
    out = RunLog(dry_run=dry_run, planned=[asdict(a) for a in actions])
    out.flags = [a.text for a in actions if a.kind == "flag"]
    out.skipped = [a.kind for a in actions if not writes and a.kind in SPOTIFY_WRITE_KINDS]
    if dry_run:
        return out
    ops = _batches(actions, writes) if pending is None else pending
    stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    # Copy intent before checkpointing, including unstamped recovery batches.
    ops = [
        {**op, "rows": [{"updated_at": stamp, **row} for row in op["rows"]]}
        if op["kind"] in {"hub", "hub_insert"}
        else dict(op)
        for op in ops
    ]
    # Bounded hub chunks give a resumed run fine-grained progress; counts land with
    # the last chunk of each original batch.
    chunked = []
    for op in ops:
        if op["kind"] == "hub" and len(op["rows"]) > CHUNK:
            parts = range(0, len(op["rows"]), CHUNK)
            for i in parts:
                last = i + CHUNK >= len(op["rows"])
                chunked.append(
                    {
                        **op,
                        "rows": op["rows"][i : i + CHUNK],
                        "counts": op["counts"] if last else {},
                    }
                )
        else:
            chunked.append(op)
    ops = chunked
    operation = "checkpoint"
    # Snapshot each playlist has after this run's own writes (None when a write returns
    # none), so the next run can reuse an unchanged smart playlist without reading it.
    written: dict[str, str | None] = {}
    try:
        if checkpoint:
            checkpoint(ops)  # durable intent BEFORE the first mutation
        for index, op in enumerate(ops):
            kind, pid = op["kind"], op.get("playlist_id")
            operation = f"{kind} {pid or op.get('table', '')}".strip()
            if deadline is not None and time.monotonic() > deadline:
                out.errors.append(
                    f"time budget reached: {len(ops) - index} operations remain pending; "
                    "rerun the same operation to resume"
                )
                return out
            started = time.monotonic()
            if kind == "hub_insert":
                hub.insert_rows(op["table"], op["rows"])
                out.applied["insert_edge"] += len(op["rows"])
            elif kind == "set_cover":
                if _cover(spotify, hub, op, ops[index + 1 :], out.flags):
                    out.applied[kind] += 1
            elif kind == "hub":
                if op["table"] == "playlists":
                    for row in op["rows"]:
                        if row.get("id") in written:
                            row["snapshot_id"] = written[row["id"]]
                hub.push(op["table"], op["rows"])
                for name, count in op["counts"].items():
                    out.applied[name] += count
            else:
                uris = op["uris"]
                if kind == "like":
                    if op.get("attempted"):
                        if market is None:
                            raise RuntimeError(
                                "Uncertain like requires a complete current observation"
                            )
                        present = {
                            (row.get("item") or row.get("track") or {}).get("uri")
                            for row in spotify.get_liked(market)
                        }
                        if not set(uris) <= present:
                            raise RuntimeError(
                                "Uncertain like outcome requires review; never silently re-like"
                            )
                    else:
                        op["attempted"] = True
                        if checkpoint:
                            checkpoint(ops[index:])
                        spotify.like(uris)
                elif kind in ("add_item", "readd_item"):
                    if op.get("attempted") and market is not None:
                        # A timeout or failure between client chunks may have added some URIs.
                        present = {
                            (r.get("item") or r.get("track") or {}).get("uri")
                            for r in spotify.get_playlist_items(pid, market)
                        }
                        uris = [uri for uri in uris if uri not in present]
                    elif not op.get("attempted"):
                        op["attempted"] = True  # only a resumed attempt re-reads the playlist
                        if checkpoint:
                            checkpoint(ops[index:])
                    written[pid] = spotify.add_items(pid, uris) if uris else None
                elif kind == "remove_item":
                    written[pid] = spotify.remove_items(pid, uris)
                elif kind == "arrange_items":
                    current = None if op.get("attempted") else op.get("current")
                    if current is not None:
                        op["attempted"] = True  # a resumed attempt re-reads the playlist
                        if checkpoint:
                            checkpoint(ops[index:])
                    written[pid] = _arrange(spotify, pid, uris, market, current)
                elif kind == "set_description":
                    spotify.set_description(pid, op["text"])
                    written[pid] = None
                elif kind == "delete_playlist":
                    spotify.unfollow_playlist(pid)
                out.applied[kind] += op["count"]
            log.info(
                "apply_op",
                op=operation,
                size=len(op.get("rows") or op.get("uris") or []),
                seconds=round(time.monotonic() - started, 2),
                remaining=len(ops) - index - 1,
            )
            if checkpoint:
                checkpoint(ops[index + 1 :])
    except Exception as e:
        # A failed batch may be partially applied. Never consume the detection baseline,
        # continue dependent writes, or clear pending intent on an uncertain outcome.
        out.errors.append(f"{operation}: {e}")
    return out


def _cover(spotify, hub, op, later, flags) -> bool:
    """Upload a playlist's cover from soma and record what Spotify shows right after on
    the run's remaining playlists write. A cover Spotify cannot take is a flag."""
    pid, name = op["playlist_id"], op.get("name") or op["playlist_id"]
    data = hub.get_file(op["file"])
    if data is None:
        problem = "is missing from soma"
    elif hashlib.sha256(data).hexdigest() != op["sha256"]:
        problem = "does not match its cover_sha256"
    else:
        problem = covers.validate(data)
    if problem:
        flags.append(f"{name}: cover {op['file']} {problem}; not uploaded")
        return False
    try:
        spotify.upload_cover(pid, data)
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status >= 500 or status == 429:
            raise  # transient: the run stops and the upload resumes from its checkpoint
        hint = " (re-consent with the ugc-image-upload scope)" if status in (401, 403) else ""
        flags.append(f"{name}: Spotify refused the cover upload, HTTP {status}{hint}")
        return False
    seen = covers.applied(op["sha256"], op.get("before"), spotify.get_cover(pid))
    for step in later:
        if step["kind"] == "hub" and step["table"] == "playlists":
            for row in step["rows"]:
                if row["id"] == pid:
                    row["cover_applied"] = seen
    return True


def _arrange(spotify, pid, desired, market, current=None):
    """Edit a playlist into `desired` order from `current` (the planned order on a first
    attempt) or else from what it holds now, so a resumed attempt simply continues.
    When the edits take more requests than rewriting the playlist, it is replaced."""
    if current is None:
        if market is None:
            raise RuntimeError("Arranging a playlist requires a complete current observation")
        current = [
            (row.get("item") or row.get("track") or {}).get("uri")
            for row in spotify.get_playlist_items(pid, market)
        ]
    if len(set(current)) != len(current) or None in current:
        raise RuntimeError("Playlist holds duplicate or unidentified items; arrangement held")
    ops = []
    for op in new_songs.edits(current, desired):
        last = ops[-1] if ops else None
        if op[0] == "add" and last and last[0] == "add" and last[2] + len(last[1]) == op[2]:
            last[1].append(op[1])  # consecutive positions: one positioned add
        else:
            ops.append((op[0], [op[1]], op[2]) if op[0] == "add" else op)
    pages = -(-len(desired) // 100)
    requests = sum(-(-len(op[1]) // 100) if op[0] != "move" else 1 for op in ops)
    if requests > max(REPLACE_AFTER, pages):
        return spotify.replace_items(pid, desired)
    snapshot = None
    for op in ops:
        if op[0] == "remove":
            snapshot = spotify.remove_items(pid, op[1])
        elif op[0] == "move":
            snapshot = spotify.reorder_items(pid, op[1], op[2])
        else:
            snapshot = spotify.add_items(pid, op[1], position=op[2])
    return snapshot
