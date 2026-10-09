"""Pure reconcile: (mirror, live) -> ordered actions. No I/O. Spec section 5.3."""

import dataclasses
from datetime import datetime, timezone

from core import metadata, rules
from core.model import Action, Live, Membership, Mirror, Song

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


def _earliest(known: str | None, observed: str) -> str:
    if not known:
        return observed
    try:
        return min(known, observed, key=datetime.fromisoformat)
    except ValueError:
        return observed


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
    *,
    source_ref: str | None = None,
    market: str | None = None,
    curation_likes: set[str] | frozenset[str] = frozenset(),
) -> list[Action]:
    if observation_only:
        return observe(mirror, live, now, source_ref=source_ref, market=market)
    today = today or now.date().isoformat()
    now_s = _iso(now)
    acts: list[Action] = []
    current_names = {p.id: p.name for p in mirror.playlists.values()}
    current_names.update({p.id: p.name for p in live.playlists.values()})
    names = {}
    for pid, name in current_names.items():
        names[name] = None if name in names else pid
    kind_of = {pid: p.kind for pid, p in mirror.playlists.items()}
    inbox_ids = {pid for pid, k in kind_of.items() if k == "inbox"}
    liked_now = set(live.liked)
    liked_before = {s.id for s in mirror.songs.values() if s.liked}
    no_isrc: list[str] = []
    flags: list[str] = []
    rule_flags: list[str] = []
    observed = metadata.observations(live)
    evidence = metadata.evidence_by_song(mirror)
    available = {
        isrc: metadata.availability(evidence[isrc], items, market)
        for isrc, items in observed.items()
    }

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
            else:
                duplicates.setdefault(key, []).append(it)

    # Seed complete identity before tombstones, including a conflicting new add/un-heart.
    # Later intended patches merge over this observation in the applicator.
    for (pid, isrc), it in actual.items():
        if (
            pid in mirror.playlists
            and (pid, isrc) not in mirror.memberships
            and (pid, isrc) not in duplicates
        ):
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
        if liked_at:
            value = datetime.fromisoformat(liked_at)
            if value.tzinfo is None:
                raise ValueError("liked_at: timezone required")
            liked_at = _iso(value.astimezone(timezone.utc))
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

    acts = metadata.merge_actions(
        acts, metadata.observation_actions(mirror, live, now, source_ref=source_ref, market=market)
    )

    # Likes and curated membership are independent. Smart rules alone decide
    # smart membership; a like transition never edits a curated playlist.
    unhearted = liked_before - liked_now
    for isrc in sorted(unhearted):
        acts.append(
            Action("upsert_song", isrc=isrc, row={"id": isrc, "liked": 0, "liked_at": None})
        )

    ambiguous_aliases = {
        isrc
        for isrc, items in observed.items()
        if len({item.track_id for item in items if item.track_id}) > 1
    }
    for isrc in sorted(ambiguous_aliases & set(curation_likes)):
        flags.append(f"{isrc}: multiple observed aliases; auto-like requires review")

    # added to curated while unliked (rule 3): like it. Tie with un-heart: un-heart won above.
    to_like: dict[str, tuple[str, str]] = {}  # isrc -> (uri, curated playlist id)
    for (pid, isrc), it in actual.items():
        if (
            kind_of.get(pid) == "curated"
            and isrc not in liked_now
            and isrc not in unhearted
            and isrc not in ambiguous_aliases
            and not any(i == isrc for _, i in duplicates)
        ):
            if isrc in curation_likes:
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

    view = dataclasses.replace(
        mirror,
        songs={i: dataclasses.replace(s) for i, s in mirror.songs.items()},
        memberships=dict(mirror.memberships),
        captures=set(mirror.captures),
    )
    for a in acts:
        if a.kind == "upsert_song":
            s = view.songs.get(a.isrc) or Song(a.isrc, 0, None, now_s)
            view.songs[a.isrc] = dataclasses.replace(s, **a.row)
        elif a.kind == "upsert_membership":
            row = {k: v for k, v in a.row.items() if k != "id"}
            view.memberships[(a.playlist_id, a.isrc)] = Membership(**row)
        elif a.kind == "delete_membership":
            view.memberships.pop((a.playlist_id, a.isrc), None)
        elif a.kind == "edge" and a.row["rel"] == "imported_from":
            view.captures.add((a.isrc, a.row["from_kind"]))

    member_ids = None

    def routing_uri(isrc, fallback=None):
        nonlocal member_ids
        verified = sorted(tid for tid, ok in available.get(isrc, {}).items() if ok)
        live_ids = sorted({it.track_id for it in observed.get(isrc, []) if it.track_id})
        ids = verified or live_ids or ([fallback] if fallback else [])
        if ids:
            return f"spotify:track:{ids[0]}"
        if member_ids is None:
            member_ids = {}
            for m in mirror.memberships.values():
                if m.spotify_track_id:
                    member_ids[m.isrc] = min(
                        member_ids.get(m.isrc, m.spotify_track_id), m.spotify_track_id
                    )
        if isrc in member_ids:
            return f"spotify:track:{member_ids[isrc]}"
        s = view.songs.get(isrc)
        return preferred_uri(s) if s else None

    # Re-liking does not resurrect a removed curated membership. Retained
    # tombstones are evidence, not an instruction to undo a user's removal.

    # inbox FIFO (rule 6)
    for pid in inbox_ids:
        lp = live.playlists.get(pid)
        if not lp or lp.items is None:
            continue
        if any(duplicate_pid == pid for duplicate_pid, _ in duplicates):
            # URI removal affects every matching occurrence, and membership rows
            # cannot choose a surviving occurrence without the owner's review.
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

    # smart materialization (rule 7), including freshly observed songs and gestures.
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
        if any(q == pid for q, _ in duplicates):
            continue  # No partial membership changes while keeper decisions are unresolved.
        have = {isrc for (q, isrc) in actual if q == pid}
        for isrc in sorted(want - have):
            uri = routing_uri(isrc)
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
        text = rules.describe(p.rule, today, {p.id: p.name for p in view.playlists.values()})
        if _strip_synced(text) != _strip_synced(lp.description):
            acts.append(Action("set_description", playlist_id=pid, text=text))

    # Replacing a recording alias requires review, even with positive availability.
    # A playable alternative is evidence for that review, never keeper approval.
    for (pid, isrc), it in actual.items():
        if it.playable is False and pid not in inbox_ids:
            flags.append(f"{pid}: {isrc} unplayable; replacement requires review")

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
        flags.append(
            f"{pid}: {isrc} has {len(drops) + 1} occurrences; keeper and metadata require review"
        )

    # mirror upkeep (rule 11) - skip playlists soft-deleted above: no plain upsert_playlist
    # row (it would overwrite deleted_at) and no membership upserts for a deleted playlist
    for lp in live.playlists.values():
        if lp.id not in mirror.playlists:
            flags.append(f"{lp.id}: playlist classification requires review")
            continue
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
            if (pid, isrc) not in duplicates and (m is None or m.spotify_track_id != it.track_id):
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
                            # A replacement alias keeps the recording's earliest add date.
                            "added_at": _earliest(m.added_at if m else None, it.added_at),
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


def observe(
    mirror: Mirror,
    live: Live,
    now: datetime,
    *,
    source_ref: str | None = None,
    market: str | None = None,
) -> list[Action]:
    """Import observations only. Never run enforcement against the review baseline."""
    stamp = _iso(now)
    acts = []
    seen = dict(live.liked)
    sources = {isrc: ("like", "liked") for isrc in live.liked}
    for lp in live.playlists.values():
        if lp.id not in mirror.playlists:
            acts.append(Action("flag", text=f"{lp.id}: playlist classification requires review"))
            continue
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
        ambiguous = set()
        for it in sorted(lp.items or [], key=lambda x: x.added_at):
            if not it.isrc:
                acts.append(Action("flag", text=f"{lp.name}: {it.name} (no ISRC or local file)"))
                continue
            seen.setdefault(it.isrc, it)
            sources.setdefault(it.isrc, ("playlist", lp.id))
            if it.isrc in members:
                ambiguous.add(it.isrc)
            members.setdefault(it.isrc, it)
        for isrc in sorted(ambiguous):
            acts.append(Action("flag", text=f"{lp.id}: {isrc} occurrence metadata requires review"))
        for isrc, it in members.items():
            if isrc in ambiguous:
                continue
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
    return metadata.merge_actions(
        acts, metadata.observation_actions(mirror, live, now, source_ref=source_ref, market=market)
    )
