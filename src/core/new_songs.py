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


def came_at(song, curated_at: str | None = None) -> str | None:
    """`curated_at`: when the song first entered a curated playlist. A like after
    that is Music Sync liking a curated song (or the rollout's bulk like), not the
    song coming your way, so only a like before it counts."""
    liked = song.liked_at if song.liked else None
    if liked and curated_at and at(curated_at) <= at(liked):
        liked = None
    times = [song.shazam_last_at, liked, song.found_at]
    return max((t for t in times if t), key=at, default=None)


def rank(
    songs: dict,
    cap: int,
    dismissed: dict[str, str] | None = None,
    curated_at: dict[str, str] | None = None,
) -> list[str]:
    """ISRCs of the songs New songs holds, newest first. `dismissed`: isrc -> when
    it was removed from New songs; it stays out while that is not older than came_at.
    `curated_at`: isrc -> when it first entered a curated playlist (see came_at)."""
    timed = []
    for isrc, song in songs.items():
        when = came_at(song, (curated_at or {}).get(isrc))
        if when is None:
            continue
        gone = (dismissed or {}).get(isrc)
        if gone and at(gone) >= at(when):
            continue
        timed.append((at(when), isrc))
    return [isrc for _, isrc in sorted(timed, reverse=True)[:cap]]


def order(songs: dict, curated_at: dict[str, str] | None = None) -> list[str]:
    """Every ISRC in `songs`, newest first by came_at (a smart playlist's order). Songs
    with no such time follow, newest first_seen first, then by ISRC."""
    timed, untimed = [], []
    for isrc, song in songs.items():
        when = came_at(song, (curated_at or {}).get(isrc))
        (timed.append((at(when), isrc)) if when else untimed.append(isrc))
    untimed.sort()
    # first_seen is a catalog stamp (UTC milliseconds): text order is time order.
    untimed.sort(key=lambda isrc: songs[isrc].first_seen or "", reverse=True)
    return [isrc for _, isrc in sorted(timed, reverse=True)] + untimed


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
    a song whose first catalog import (an imported_from edge with created_row 1)
    came from a curated playlist or the inbox was found at its earliest add to any
    curated playlist or the inbox (Spotify's dates, removed memberships included).
    A song keeps a found_at that is already no later than that."""
    kinds = {pid: p.kind for pid, p in playlists.items()}
    first = {}
    for m in memberships:
        if kinds.get(m.playlist_id) in ("curated", "inbox") and m.added_at:
            if m.isrc not in first or at(m.added_at) < at(first[m.isrc]):
                first[m.isrc] = m.added_at
    out = {}
    for e in edges:
        detail = e.get("detail")
        detail = json.loads(detail) if isinstance(detail, str) else detail or {}
        isrc = e.get("to_ref")
        when = first.get(isrc)
        if (
            e.get("from_kind") == "playlist"
            and detail.get("created_row") == 1
            and kinds.get(e.get("from_ref")) in ("curated", "inbox")
            and isrc in songs
            and when
            and (not songs[isrc].found_at or at(when) < at(songs[isrc].found_at))
        ):
            moment = at(when).astimezone(timezone.utc)
            out[isrc] = moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"
    return out


def first_curated(memberships, kind_of: dict) -> dict[str, str]:
    """isrc -> the earliest add time of any curated playlist membership, live or removed."""
    out = {}
    for m in memberships:
        if kind_of.get(m.playlist_id) == "curated" and m.added_at:
            if m.isrc not in out or at(m.added_at) < at(out[m.isrc]):
                out[m.isrc] = m.added_at
    return out
