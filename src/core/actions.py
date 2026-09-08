"""Apply planned actions: Spotify writes batched per playlist, hub rows grouped per table."""

from collections import defaultdict
from dataclasses import dataclass, field

import structlog

from core.hub import HubError
from core.model import Action

log = structlog.get_logger()
HUB_TABLE = {
    "upsert_song": "songs",
    "upsert_playlist": "playlists",
    "upsert_membership": "playlist_songs",
    "delete_membership": "playlist_songs",
    "edge": "provenance",
}


@dataclass
class RunLog:
    applied: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    skipped: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    dry_run: bool = False
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [f"{k}: {v}" for k, v in sorted(self.applied.items()) if v]
        lines += [f"flag: {f}" for f in self.flags] + [f"error: {e}" for e in self.errors]
        return ("DRY RUN\n" if self.dry_run else "") + ("\n".join(lines) or "nothing to do")


SPOTIFY_WRITE_KINDS = {
    "like",
    "add_item",
    "readd_item",
    "remove_item",
    "set_description",
    "delete_playlist",
}


def apply(actions: list[Action], spotify, hub, dry_run: bool, writes: bool = True) -> RunLog:
    out = RunLog(dry_run=dry_run)
    likes: list[str] = []
    adds: dict[str, list[str]] = defaultdict(list)
    removes: dict[str, list[str]] = defaultdict(list)
    readds: dict[str, list[str]] = defaultdict(list)
    descs: dict[str, str] = {}
    deletes: list[str] = []
    rows: dict[str, dict[str, dict]] = defaultdict(dict)  # table -> id -> merged row
    for a in actions:
        out.applied[a.kind] += 1
        if not writes and a.kind in SPOTIFY_WRITE_KINDS:
            out.skipped.append(f"{a.kind} {a.playlist_id} {a.uri}")
            continue
        if a.kind == "like":
            likes.append(a.uri)
        elif a.kind == "add_item":
            adds[a.playlist_id].append(a.uri)
        elif a.kind == "remove_item":
            removes[a.playlist_id].append(a.uri)
        elif a.kind == "readd_item":
            readds[a.playlist_id].append(a.uri)
        elif a.kind == "set_description":
            descs[a.playlist_id] = a.text
        elif a.kind == "delete_playlist":
            deletes.append(a.playlist_id)
        elif a.kind in HUB_TABLE:
            rows[HUB_TABLE[a.kind]].setdefault(a.row["id"], {}).update(a.row)
        elif a.kind == "flag":
            out.flags.append(a.text)
    if dry_run:
        return out
    if likes:
        _safe(out, "like", lambda: spotify.like(sorted(set(likes))))
    for pid, uris in adds.items():
        _safe(out, f"add {pid}", lambda: spotify.add_items(pid, uris))
    for pid, uris in removes.items():
        _safe(out, f"remove {pid}", lambda: spotify.remove_items(pid, uris))
    for pid, uris in readds.items():
        _safe(out, f"readd {pid}", lambda: spotify.add_items(pid, uris))
    for pid, text in descs.items():
        _safe(out, f"describe {pid}", lambda: spotify.set_description(pid, text))
    for pid in deletes:
        _safe(out, f"unfollow {pid}", lambda: spotify.unfollow_playlist(pid))
    for table in ("songs", "playlists", "playlist_songs", "provenance"):
        if rows.get(table):
            try:
                hub.push(table, list(rows[table].values()))
            except HubError as e:
                out.errors.append(f"hub {table}: {e}")
    return out


def _safe(out: RunLog, what: str, fn) -> None:
    try:
        fn()
    except Exception as e:  # one playlist failing must not stop the others
        log.warning("spotify_write_failed", what=what, error=str(e))
        out.errors.append(f"spotify {what}: {e}")
