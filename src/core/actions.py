"""Apply a plan in ordered batches, stopping at the first failed checkpoint."""

import json
from collections import defaultdict
from dataclasses import asdict, dataclass, field

from core.model import Action

HUB_TABLE = {
    "upsert_song": "songs",
    "upsert_playlist": "playlists",
    "upsert_membership": "playlist_songs",
    "delete_membership": "playlist_songs",
    "edge": "provenance",
}
SPOTIFY_WRITE_KINDS = {
    "like",
    "add_item",
    "readd_item",
    "remove_item",
    "set_description",
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

    def summary(self) -> str:
        lines = [f"applied {k}: {v}" for k, v in sorted(self.applied.items()) if v]
        for category, predicate in (
            ("Planned Spotify mutations", lambda a: a["kind"] in SPOTIFY_WRITE_KINDS),
            ("Planned mirror patches", lambda a: a["kind"] in HUB_TABLE),
        ):
            details = [a for a in self.planned if predicate(a)]
            lines.append(f"{category}: {len(details)}")
            lines.extend(json.dumps(a, ensure_ascii=False, sort_keys=True) for a in details)
        lines += [f"flag: {f}" for f in self.flags] + [f"error: {e}" for e in self.errors]
        return ("DRY RUN\n" if self.dry_run else "") + "\n".join(lines)


def _batches(actions: list[Action], writes: bool) -> list[dict]:
    ops = []
    for kind in (
        "like",
        "add_item",
        "remove_item",
        "readd_item",
        "set_description",
        "delete_playlist",
    ):
        groups = defaultdict(list)
        for a in actions:
            if writes and a.kind == kind:
                groups[a.playlist_id].append(a)
        for pid, group in groups.items():
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
            if HUB_TABLE.get(a.kind) == table:
                rows.setdefault(a.row["id"], {}).update(a.row)
                counts[a.kind] += 1
        if rows:
            ops.append(
                {"kind": "hub", "table": table, "rows": list(rows.values()), "counts": dict(counts)}
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
) -> RunLog:
    out = RunLog(dry_run=dry_run, planned=[asdict(a) for a in actions])
    out.flags = [a.text for a in actions if a.kind == "flag"]
    out.skipped = [a.kind for a in actions if not writes and a.kind in SPOTIFY_WRITE_KINDS]
    if dry_run:
        return out
    ops = _batches(actions, writes) if pending is None else pending
    operation = "checkpoint"
    try:
        if checkpoint:
            checkpoint(ops)  # durable intent BEFORE the first mutation
        for index, op in enumerate(ops):
            kind, pid = op["kind"], op.get("playlist_id")
            operation = f"{kind} {pid or op.get('table', '')}".strip()
            if kind == "hub":
                hub.push(op["table"], op["rows"])
                for name, count in op["counts"].items():
                    out.applied[name] += count
            else:
                uris = op["uris"]
                if kind == "like":
                    spotify.like(uris)
                elif kind in ("add_item", "readd_item"):
                    if market is not None:
                        # A timeout or failure between client chunks may have added some URIs.
                        present = {
                            (r.get("item") or r.get("track") or {}).get("uri")
                            for r in spotify.get_playlist_items(pid, market)
                        }
                        uris = [uri for uri in uris if uri not in present]
                    if uris:
                        spotify.add_items(pid, uris)
                elif kind == "remove_item":
                    spotify.remove_items(pid, uris)
                elif kind == "set_description":
                    spotify.set_description(pid, op["text"])
                elif kind == "delete_playlist":
                    spotify.unfollow_playlist(pid)
                out.applied[kind] += op["count"]
            if checkpoint:
                checkpoint(ops[index + 1 :])
    except Exception as e:
        # A failed batch may be partially applied. Never consume the detection baseline,
        # continue dependent writes, or clear pending intent on an uncertain outcome.
        out.errors.append(f"{operation}: {e}")
    return out
