"""New songs: the songs that most recently came your way, newest first. Pure.

A song comes your way at the latest of its Shazam (shazam_last_at), its like
(liked_at, while liked) and found_at (you found it yourself). The inbox holds
the `cap` songs with the latest such time; one removed from it by hand stays
out until it comes your way again.
"""

import json
from datetime import datetime, timezone


def at(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def came_at(song) -> str | None:
    times = [song.shazam_last_at, song.liked_at if song.liked else None, song.found_at]
    return max((t for t in times if t), key=at, default=None)


def rank(songs: dict, cap: int, dismissed: dict[str, str] | None = None) -> list[str]:
    """ISRCs of the songs New songs holds, newest first. `dismissed`: isrc -> when
    it was removed from New songs; it stays out while that is not older than came_at."""
    timed = []
    for isrc, song in songs.items():
        when = came_at(song)
        if when is None:
            continue
        gone = (dismissed or {}).get(isrc)
        if gone and at(gone) >= at(when):
            continue
        timed.append((at(when), isrc))
    return [isrc for _, isrc in sorted(timed, reverse=True)[:cap]]


def edits(current: list[str], desired: list[str]) -> list[tuple]:
    """Playlist operations turning `current` into `desired` (both unique URIs):
    ("remove", uris), then per position top-down ("move", from, before) or
    ("add", uri, position). Indexes are the playlist's at that operation."""
    keep = set(desired)
    ops, order = [], [u for u in current if u in keep]
    gone = [u for u in current if u not in keep]
    if gone:
        ops.append(("remove", gone))
    for i, uri in enumerate(desired):
        if i < len(order) and order[i] == uri:
            continue
        if uri in order:
            j = order.index(uri)
            ops.append(("move", j, i))
            order.insert(i, order.pop(j))
        else:
            ops.append(("add", uri, i))
            order.insert(i, uri)
    return ops


def found_from_history(songs: dict, playlists: dict, memberships: list, edges: list) -> dict:
    """found_at for songs you brought into the catalog yourself before it was kept:
    the first catalog import of a song from a curated playlist or the inbox
    (an imported_from edge with created_row 1) found it at that membership's add
    time. Songs that already have found_at are left alone."""
    added = {(m.playlist_id, m.isrc): m.added_at for m in memberships if m.added_at}
    out = {}
    for e in edges:
        detail = e.get("detail")
        detail = json.loads(detail) if isinstance(detail, str) else detail or {}
        isrc, pid = e.get("to_ref"), e.get("from_ref")
        playlist = playlists.get(pid)
        when = added.get((pid, isrc))
        if (
            e.get("from_kind") == "playlist"
            and detail.get("created_row") == 1
            and playlist
            and playlist.kind in ("curated", "inbox")
            and isrc in songs
            and not songs[isrc].found_at
            and when
        ):
            moment = at(when).astimezone(timezone.utc)
            out[isrc] = moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"
    return out
