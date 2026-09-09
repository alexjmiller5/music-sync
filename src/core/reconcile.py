"""Pure reconcile: (mirror, live) -> ordered actions. No I/O. Spec section 5.3."""

import dataclasses
from datetime import datetime, timedelta

from core import rules
from core.model import Action, Live, Mirror, Song

ORDER = [
    "like",
    "add_item",
    "remove_item",
    "readd_item",
    "set_description",
    "delete_playlist",
    "upsert_song",
    "upsert_playlist",
    "upsert_membership",
    "delete_membership",
    "edge",
    "flag",
]


def preferred_uri(song: Song) -> str | None:
    return f"spotify:track:{song.spotify_ids[0]}" if song.spotify_ids else None


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _strip_synced(desc: str | None) -> str:
    return (desc or "").split(" · synced ")[0]


def plan(
    mirror: Mirror,
    live: Live,
    now: datetime,
    inbox_cap: int = 100,
    undo_days: int = 7,
    today: str | None = None,
    observation_only: bool = False,
) -> list[Action]:
    if observation_only:
        return observe(mirror, live, now)
    today = today or now.date().isoformat()
    now_s = _iso(now)
    acts: list[Action] = []
    names = {p.name: p.id for p in mirror.playlists.values()}
    names.update({p.name: p.id for p in live.playlists.values()})
    kind_of = {pid: "curated" for pid in live.playlists}
    kind_of.update({pid: p.kind for pid, p in mirror.playlists.items()})
    inbox_ids = {pid for pid, k in kind_of.items() if k == "inbox"}
    liked_now = set(live.liked)
    liked_before = {s.id for s in mirror.songs.values() if s.liked}
    no_isrc: list[str] = []
    flags: list[str] = []
    rule_flags: list[str] = []

    # Index earliest observation; decide duplicate repairs after final membership.
    actual = {}
    duplicates = {}
    for lp in live.playlists.values():
        for it in sorted(lp.items or [], key=lambda x: x.added_at):
            if not it.isrc:
                no_isrc.append(f"{lp.name}: {it.name} (no ISRC or local file)")
                continue
            key = (lp.id, it.isrc)
            if key not in actual:
                actual[key] = it
            elif lp.id not in inbox_ids:
                duplicates.setdefault(key, []).append(it)

    # Seed complete identity before tombstones, including a conflicting new add/un-heart.
    # Later intended patches merge over this observation in the applicator.
    for (pid, isrc), it in actual.items():
        if (pid, isrc) not in mirror.memberships:
            acts.append(
                Action(
                    "upsert_membership",
                    playlist_id=pid,
                    isrc=isrc,
                    row={
                        "id": f"{pid}:{isrc}",
                        "playlist_id": pid,
                        "isrc": isrc,
                        "spotify_track_id": it.track_id,
                        "added_at": it.added_at,
                        "deleted_at": None,
                    },
                )
            )

    # songs: new rows + liked transitions (rules 1, 2)
    seen_isrcs = liked_now | {isrc for (_, isrc) in actual}
    for isrc in sorted(seen_isrcs):
        li = live.liked.get(isrc)
        liked = 1 if li else 0
        liked_at = li.added_at if li else None
        s = mirror.songs.get(isrc)
        if s is None:
            acts.append(
                Action(
                    "upsert_song",
                    isrc=isrc,
                    row={"id": isrc, "liked": liked, "liked_at": liked_at, "first_seen": now_s},
                )
            )
            src = next(
                (
                    (pid, it)
                    for (pid, i), it in actual.items()
                    if i == isrc and pid not in inbox_ids
                ),
                None,
            )
            from_kind, from_ref = (
                ("playlist", src[0])
                if src
                else ("like", "liked")
                if li
                else ("playlist", next(pid for (pid, i) in actual if i == isrc))
            )
            acts.append(
                Action(
                    "edge",
                    isrc=isrc,
                    row={
                        "id": f"{from_kind}:{from_ref}:{isrc}",
                        "from_kind": from_kind,
                        "from_ref": from_ref,
                        "to_kind": "songs",
                        "to_ref": isrc,
                        "rel": "imported_from",
                        "asserted_by": "music-sync",
                        "detail": {"created_row": 1},
                    },
                )
            )
        elif (s.liked, s.liked_at) != (liked, liked_at) and isrc not in (liked_before - liked_now):
            acts.append(
                Action(
                    "upsert_song", isrc=isrc, row={"id": isrc, "liked": liked, "liked_at": liked_at}
                )
            )

    # un-heart (rule 4): remove from every non-inbox playlist it is in - both a playlist
    # pulled this run (via `actual`) and one skipped this run (via the mirror membership,
    # since `actual` has no entry at all for a playlist whose items weren't fetched)
    unhearted = liked_before - liked_now
    for isrc in sorted(unhearted):
        acts.append(
            Action("upsert_song", isrc=isrc, row={"id": isrc, "liked": 0, "liked_at": None})
        )
        for (pid, i), it in list(actual.items()):
            if i == isrc and pid not in inbox_ids:
                acts.append(
                    Action(
                        "remove_item", playlist_id=pid, isrc=isrc, uri=it.uri, reason="un-hearted"
                    )
                )
                acts.append(
                    Action(
                        "delete_membership",
                        playlist_id=pid,
                        isrc=isrc,
                        row={"id": f"{pid}:{isrc}", "deleted_at": now_s},
                    )
                )
                actual.pop((pid, i))
        for (pid, i), mem in mirror.memberships.items():
            if i != isrc or pid in inbox_ids:
                continue
            lp = live.playlists.get(pid)
            if lp is None or lp.items is not None:
                continue  # fetched this run: already handled above via `actual`
            acts.append(
                Action(
                    "remove_item",
                    playlist_id=pid,
                    isrc=isrc,
                    uri=f"spotify:track:{mem.spotify_track_id}",
                    reason="un-hearted",
                )
            )
            acts.append(
                Action(
                    "delete_membership",
                    playlist_id=pid,
                    isrc=isrc,
                    row={"id": f"{pid}:{isrc}", "deleted_at": now_s},
                )
            )

    # added to curated while unliked (rule 3): like it. Tie with un-heart: un-heart won above.
    to_like: dict[str, tuple[str, str]] = {}  # isrc -> (uri, curated playlist id)
    for (pid, isrc), it in actual.items():
        if kind_of.get(pid) == "curated" and isrc not in liked_now and isrc not in unhearted:
            if (pid, isrc) not in mirror.memberships:
                to_like[isrc] = (it.uri, pid)
    for isrc, (uri, pid) in sorted(to_like.items()):
        acts.append(Action("like", isrc=isrc, uri=uri, reason="in curated playlist"))
        acts.append(
            Action("upsert_song", isrc=isrc, row={"id": isrc, "liked": 1, "liked_at": now_s})
        )
        # only for songs that already existed: a brand-new song already got its
        # created_row=1 edge from the new-song branch above (rule 1); a second
        # edge here would merge over it and regress created_row to 0.
        if isrc in mirror.songs and (isrc, "playlist") not in mirror.captures:
            acts.append(
                Action(
                    "edge",
                    isrc=isrc,
                    row={
                        "id": f"playlist:{pid}:{isrc}",
                        "from_kind": "playlist",
                        "from_ref": pid,
                        "to_kind": "songs",
                        "to_ref": isrc,
                        "rel": "imported_from",
                        "asserted_by": "music-sync",
                        "detail": {"created_row": 0},
                    },
                )
            )
    liked_effective = (liked_now | set(to_like)) - unhearted

    # undo (rule 5)
    cutoff = _iso(now - timedelta(days=undo_days))
    for m in mirror.deleted_memberships:
        s = mirror.songs.get(m.isrc)
        if (
            m.isrc in liked_now
            and s
            and not s.liked
            and kind_of.get(m.playlist_id) == "curated"
            and (m.deleted_at or "") >= cutoff
            and (m.playlist_id, m.isrc) not in actual
            and s.spotify_ids
        ):
            uri = preferred_uri(s)
            acts.append(
                Action(
                    "add_item",
                    playlist_id=m.playlist_id,
                    isrc=m.isrc,
                    uri=uri,
                    reason="undo un-heart",
                )
            )
            acts.append(
                Action(
                    "upsert_membership",
                    playlist_id=m.playlist_id,
                    isrc=m.isrc,
                    row={
                        "id": m.id,
                        "playlist_id": m.playlist_id,
                        "isrc": m.isrc,
                        "spotify_track_id": uri.split(":")[-1],
                        "added_at": now_s,
                        "deleted_at": None,
                    },
                )
            )

    # inbox FIFO (rule 6)
    for pid in inbox_ids:
        lp = live.playlists.get(pid)
        if not lp or lp.items is None:
            continue
        with_isrc = sorted([it for it in lp.items if it.isrc], key=lambda x: x.added_at)
        for it in with_isrc[: max(0, len(with_isrc) - inbox_cap)]:
            acts.append(
                Action(
                    "remove_item",
                    playlist_id=pid,
                    isrc=it.isrc,
                    uri=it.uri,
                    reason="inbox overflow",
                )
            )
            acts.append(
                Action(
                    "delete_membership",
                    playlist_id=pid,
                    isrc=it.isrc,
                    row={"id": f"{pid}:{it.isrc}", "deleted_at": now_s},
                )
            )
            actual.pop((pid, it.isrc), None)

    # smart materialization (rule 7), on a mirror view that reflects this run's liked state.
    # Songs are copied (not shared) so mutating .liked here never touches the caller's mirror.
    view = Mirror(
        {isrc: dataclasses.replace(s) for isrc, s in mirror.songs.items()},
        mirror.playlists,
        dict(mirror.memberships),
        [],
        mirror.captures,
    )
    for isrc in liked_effective:
        if isrc in view.songs:
            view.songs[isrc].liked = 1
    for isrc in unhearted:
        if isrc in view.songs:
            view.songs[isrc].liked = 0
    rule_errors: dict[str, str] = {}
    desired = rules.evaluate(view, names, rule_errors)
    for pid, msg in rule_errors.items():
        name = mirror.playlists[pid].name if pid in mirror.playlists else pid
        rule_flags.append(f"{name}: rule error: {msg}")
    for pid, want in desired.items():
        lp = live.playlists.get(pid)
        p = mirror.playlists[pid]
        if lp is None or lp.items is None:
            continue
        if p.pinned == 0 and p.expires_at and p.expires_at < now_s:
            continue
        have = {isrc for (q, isrc) in actual if q == pid}
        for isrc in sorted(want - have):
            uri = preferred_uri(mirror.songs[isrc]) if isrc in mirror.songs else None
            if not uri:
                flags.append(f"{p.name}: {isrc} has no Spotify id yet")
                continue
            acts.append(
                Action("add_item", playlist_id=pid, isrc=isrc, uri=uri, reason="matches rule")
            )
            acts.append(
                Action(
                    "upsert_membership",
                    playlist_id=pid,
                    isrc=isrc,
                    row={
                        "id": f"{pid}:{isrc}",
                        "playlist_id": pid,
                        "isrc": isrc,
                        "spotify_track_id": uri.split(":")[-1],
                        "added_at": now_s,
                        "deleted_at": None,
                    },
                )
            )
        for isrc in sorted(have - want):
            acts.append(
                Action(
                    "remove_item",
                    playlist_id=pid,
                    isrc=isrc,
                    uri=actual[(pid, isrc)].uri,
                    reason="removed by rule",
                )
            )
            acts.append(
                Action(
                    "delete_membership",
                    playlist_id=pid,
                    isrc=isrc,
                    row={"id": f"{pid}:{isrc}", "deleted_at": now_s},
                )
            )
            actual.pop((pid, isrc))
        text = rules.describe(p.rule, today)
        if _strip_synced(text) != _strip_synced(lp.description):
            acts.append(Action("set_description", playlist_id=pid, text=text))

    # unplayable relink (rule 8): inbox is exempt, same as dedupe (rule 9)
    for (pid, isrc), it in list(actual.items()):
        if it.playable or pid in inbox_ids:
            continue
        s = mirror.songs.get(isrc)
        alt = (
            s.spotify_ids[0]
            if s and s.spotify_playable and s.spotify_ids and s.spotify_ids[0] != it.track_id
            else None
        )
        if alt:
            actual[(pid, isrc)] = dataclasses.replace(
                it, track_id=alt, uri=f"spotify:track:{alt}", playable=True
            )
            acts.append(
                Action(
                    "remove_item",
                    playlist_id=pid,
                    isrc=isrc,
                    uri=it.uri,
                    reason="unplayable, relinking",
                )
            )
            acts.append(
                Action(
                    "add_item",
                    playlist_id=pid,
                    isrc=isrc,
                    uri=f"spotify:track:{alt}",
                    reason="relinked",
                )
            )
            acts.append(
                Action(
                    "upsert_membership",
                    playlist_id=pid,
                    isrc=isrc,
                    row={
                        "id": f"{pid}:{isrc}",
                        "playlist_id": pid,
                        "isrc": isrc,
                        "spotify_track_id": alt,
                        "added_at": it.added_at,
                        "deleted_at": None,
                    },
                )
            )
        else:
            flags.append(
                f"{mirror.playlists[pid].name if pid in mirror.playlists else pid}: {it.name} [{isrc}] unplayable, no alternative"
            )

    # ephemeral expiry (rule 10)
    expired_ids: set[str] = set()
    for p in mirror.playlists.values():
        if (
            p.kind == "smart"
            and p.pinned == 0
            and p.expires_at
            and p.expires_at < now_s
            and p.id in live.playlists
        ):
            expired_ids.add(p.id)
            acts.append(Action("delete_playlist", playlist_id=p.id, reason="ephemeral expired"))
            acts.append(
                Action("upsert_playlist", playlist_id=p.id, row={"id": p.id, "deleted_at": now_s})
            )

    for (pid, isrc), drops in duplicates.items():
        if pid in expired_ids:
            continue
        kept = actual.get((pid, isrc))
        removed_uris = {it.uri for it in drops}
        for uri in sorted(removed_uris):
            acts.append(
                Action("remove_item", playlist_id=pid, isrc=isrc, uri=uri, reason="duplicate isrc")
            )
        if kept and kept.uri in removed_uris:
            acts.append(
                Action(
                    "readd_item",
                    playlist_id=pid,
                    isrc=isrc,
                    uri=kept.uri,
                    reason="duplicate isrc, same uri",
                )
            )

    # mirror upkeep (rule 11) - skip playlists soft-deleted above: no plain upsert_playlist
    # row (it would overwrite deleted_at) and no membership upserts for a deleted playlist
    for lp in live.playlists.values():
        if lp.id in expired_ids:
            continue
        row = {
            "id": lp.id,
            "name": lp.name,
            "description": lp.description,
            "snapshot_id": lp.snapshot_id,
            "last_reconciled": now_s,
        }
        if lp.id not in mirror.playlists:
            row.update({"kind": "curated", "pinned": 1})
        if lp.items is not None:
            row["track_count"] = len(lp.items)
        acts.append(Action("upsert_playlist", playlist_id=lp.id, row=row))
        if lp.items is None:
            continue
        for (pid, isrc), it in actual.items():
            if pid != lp.id:
                continue
            m = mirror.memberships.get((pid, isrc))
            if m is None or m.spotify_track_id != it.track_id:
                acts.append(
                    Action(
                        "upsert_membership",
                        playlist_id=pid,
                        isrc=isrc,
                        row={
                            "id": f"{pid}:{isrc}",
                            "playlist_id": pid,
                            "isrc": isrc,
                            "spotify_track_id": it.track_id,
                            "added_at": it.added_at,
                            "deleted_at": None,
                        },
                    )
                )
        gone = {isrc for (pid, isrc) in mirror.memberships if pid == lp.id} - {
            isrc for (pid, isrc) in actual if pid == lp.id
        }
        already = {a.row["id"] for a in acts if a.kind == "delete_membership"}
        for isrc in sorted(gone):
            if f"{lp.id}:{isrc}" not in already:
                acts.append(
                    Action(
                        "delete_membership",
                        playlist_id=lp.id,
                        isrc=isrc,
                        row={"id": f"{lp.id}:{isrc}", "deleted_at": now_s},
                    )
                )

    if no_isrc:
        acts.append(
            Action(
                "flag",
                text="Items without ISRC (skipped): " + "; ".join(sorted(set(no_isrc))),
                reason="no_isrc",
            )
        )
    for f in flags:
        acts.append(Action("flag", text=f, reason="attention"))
    for f in rule_flags:
        acts.append(Action("flag", text=f, reason="rule"))
    rank = {k: i for i, k in enumerate(ORDER)}
    return sorted(
        (a for a in acts if not (a.kind == "upsert_membership" and a.playlist_id in expired_ids)),
        key=lambda a: rank[a.kind],
    )


def observe(mirror: Mirror, live: Live, now: datetime) -> list[Action]:
    """Import observations only. Never run enforcement against the review baseline."""
    stamp = _iso(now)
    acts = []
    seen = dict(live.liked)
    sources = {isrc: ("like", "liked") for isrc in live.liked}
    for lp in live.playlists.values():
        row = {
            "id": lp.id,
            "name": lp.name,
            "description": lp.description,
            "snapshot_id": lp.snapshot_id,
            "last_reconciled": stamp,
        }
        if lp.id not in mirror.playlists:
            row.update(kind="curated", pinned=1)
        if lp.items is not None:
            row["track_count"] = len(lp.items)
        acts.append(Action("upsert_playlist", playlist_id=lp.id, row=row))
        members = {}
        for it in sorted(lp.items or [], key=lambda x: x.added_at):
            if not it.isrc:
                acts.append(Action("flag", text=f"{lp.name}: {it.name} (no ISRC or local file)"))
                continue
            seen.setdefault(it.isrc, it)
            sources.setdefault(it.isrc, ("playlist", lp.id))
            members.setdefault(it.isrc, it)
        for isrc, it in members.items():
            acts.append(
                Action(
                    "upsert_membership",
                    playlist_id=lp.id,
                    isrc=isrc,
                    row={
                        "id": f"{lp.id}:{isrc}",
                        "playlist_id": lp.id,
                        "isrc": isrc,
                        "spotify_track_id": it.track_id,
                        "added_at": it.added_at,
                        "deleted_at": None,
                    },
                )
            )
        if lp.items is not None:
            for pid, isrc in mirror.memberships:
                if pid == lp.id and isrc not in members:
                    acts.append(
                        Action(
                            "delete_membership",
                            playlist_id=pid,
                            isrc=isrc,
                            row={"id": f"{pid}:{isrc}", "deleted_at": stamp},
                        )
                    )
    for isrc in sorted(set(seen) | set(mirror.songs)):
        li = live.liked.get(isrc)
        row = {"id": isrc, "liked": int(li is not None), "liked_at": li.added_at if li else None}
        if isrc not in mirror.songs:
            row["first_seen"] = stamp
            kind, ref = sources[isrc]
            acts.append(
                Action(
                    "edge",
                    isrc=isrc,
                    row={
                        "id": f"{kind}:{ref}:{isrc}",
                        "from_kind": kind,
                        "from_ref": ref,
                        "to_kind": "songs",
                        "to_ref": isrc,
                        "rel": "imported_from",
                        "asserted_by": "music-sync",
                        "detail": {"created_row": 1},
                    },
                )
            )
        acts.append(Action("upsert_song", isrc=isrc, row=row))
    return acts
