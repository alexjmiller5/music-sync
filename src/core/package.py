"""One-time rollout package: owner decisions applied to one complete observation.

`build` turns a dry-run snapshot, the owner's decision manifest and a private spec
into an exact package. `simulate` applies it to an observation without I/O so a
dry run can show what the first reconciliation after it would do. `apply` runs
inside the serialized worker: it validates every precondition against a fresh
complete pull before the first write, checkpoints progress in the pending key,
and verifies the result by reading Spotify back. Personal content (playlist and
song identities) lives in the package and spec, never in this module.

Spotify removes playlist items by URI only, so dropping one occurrence of a
repeated URI means deleting the URI and re-inserting the kept occurrence at its
final position. Every intermediate playlist state is known in advance, which
makes a resumed run recognise exactly where it stopped.
"""

import copy
import gzip
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone

from core import archive, curation, mirror as mirror_mod, rules
from core.model import Mirror, Playlist

VERSION = 1
ACCEPTED = ("approved", "accepted", "auto_album_first")
CHUNK = 100


class PackageError(RuntimeError):
    pass


def digest(package: dict) -> str:
    text = json.dumps(package, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _same_time(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    return datetime.fromisoformat(a) == datetime.fromisoformat(b)


# --- playlist edits -------------------------------------------------------------


def edit_ops(before: list[str], after: list[str]) -> list[dict]:
    """URI-wide deletes, then inserts at final positions in ascending order."""
    count_before, count_after = Counter(before), Counter(after)
    deleted = list(dict.fromkeys(u for u in before if count_after[u] < count_before[u]))
    gone = set(deleted)
    survivors = [u for u in before if u not in gone]
    inserted = [i for i, u in enumerate(after) if u in gone or count_before[u] == 0]
    marked = set(inserted)
    if [u for i, u in enumerate(after) if i not in marked] != survivors:
        raise PackageError("playlist edits may only remove and insert, never reorder or duplicate")
    ops = [{"op": "delete", "uris": deleted[i : i + CHUNK]} for i in range(0, len(deleted), CHUNK)]
    run: list[int] = []
    for index in inserted + [None]:
        if run and (index is None or index != run[-1] + 1 or len(run) == CHUNK):
            ops.append({"op": "insert", "position": run[0], "uris": [after[i] for i in run]})
            run = []
        if index is not None:
            run.append(index)
    return ops


def states(before: list[str], ops: list[dict]) -> list[list[str]]:
    out = [list(before)]
    for op in ops:
        current = list(out[-1])
        if op["op"] == "delete":
            drop = set(op["uris"])
            current = [u for u in current if u not in drop]
        else:
            current[op["position"] : op["position"]] = op["uris"]
        out.append(current)
    return out


def progress(current: list[str], before: list[str], ops: list[dict]) -> int | None:
    """Number of ops already applied, or None when the playlist changed independently."""
    for done, state in reversed(list(enumerate(states(before, ops)))):
        if state == current:
            return done
    return None


# --- build ------------------------------------------------------------------------


def _status_ok(group: dict) -> bool:
    return str(group.get("status", "")).startswith(ACCEPTED)


def _items(live, pid):
    playlist = live.playlists.get(pid)
    return playlist.items if playlist and playlist.items is not None else None


def _locate(items, occurrence):
    """Fresh index of an archived occurrence: same position first, else a unique match."""
    want, when, index = (
        occurrence.get("spotify_id"),
        occurrence.get("added_at"),
        occurrence["index"],
    )

    def same(item):
        ids = {item.track_id, item.linked_from_id}
        return (want is None or want in ids) and _same_time(item.added_at, when)

    if index < len(items) and same(items[index]):
        return index
    matches = [i for i, item in enumerate(items) if same(item)]
    return matches[0] if len(matches) == 1 else None


def _track_meta(item) -> dict:
    return {
        "isrc": item.isrc,
        "track_id": item.track_id,
        "name": item.name,
        "artists": item.artists,
        "album": item.album,
        "album_year": item.album_year,
        "duration_ms": item.duration_ms,
        "playable": item.playable,
    }


def build(live, mirror: Mirror, decisions: dict, spec: dict, *, observed_at: str) -> dict:
    """Exact package plus the review report. Pure: no network, no clock."""
    suffix = spec.get("rename_suffix", " (pre-sync)")
    frozen = {s["replaces"] for s in spec["smart"] if s.get("replaces")}
    classify = spec.get("classify", {})
    kinds = {pid: p.kind for pid, p in mirror.playlists.items()}
    kinds.update(classify)
    curated = {pid for pid, kind in kinds.items() if kind == "curated" and pid in live.playlists}
    tracks: dict[str, dict] = {}
    for playlist in live.playlists.values():
        for item in playlist.items or []:
            if item.uri and item.isrc:
                tracks.setdefault(item.uri, _track_meta(item))
    for item in live.liked.values():
        if item.uri:
            tracks.setdefault(item.uri, _track_meta(item))

    held, stale, skipped_frozen = [], [], []
    removals = defaultdict(dict)  # pid -> index -> reason
    inserts = defaultdict(dict)  # pid -> index -> [uris]
    keeper_of: dict[str, tuple[str, str, str]] = {}  # superseded isrc -> (keeper isrc, uri, why)
    alias_target: dict[str, str] = {}  # isrc -> chosen track id
    exceptions = []

    def external(keeper, title, artists):
        uri = f"spotify:track:{keeper['spotify_id']}"
        tracks.setdefault(
            uri,
            {
                "isrc": keeper.get("isrc"),
                "track_id": keeper["spotify_id"],
                "name": keeper.get("title") or title,
                "artists": keeper.get("artists") or artists,
                "album": keeper.get("album"),
                "album_year": None,
                "duration_ms": keeper.get("duration_ms"),
                "playable": keeper.get("playable_2026_10_08"),
            },
        )
        return uri

    groups = [("identical", g) for g in decisions.get("identical_id_groups", [])] + [
        ("variant", g) for g in decisions.get("variant_groups", [])
    ]
    for family, group in groups:
        label = f"{group.get('title')} / {', '.join(group.get('artists') or [])}"
        if group.get("status") == "keep_all":
            continue
        if not _status_ok(group):
            held.append({"kind": "recording_choice", "group": label, "ask": group.get("ask_id")})
            continue
        pid, keeper = group["playlist_id"], group.get("keeper") or {}
        items = _items(live, pid)
        located = None if items is None else [_locate(items, o) for o in group["occurrences"]]
        if located is None or None in located:
            stale.append({"group": label, "playlist_id": pid, "reason": "occurrence moved"})
            continue
        by_pointer = {o["pointer"]: i for o, i in zip(group["occurrences"], located)}
        drop = [by_pointer[r["pointer"]] for r in group.get("remove", [])]
        isrcs = {items[i].isrc for i in located}
        target_uri = None
        if keeper.get("action") == "add_target_then_remove_sources":
            target_uri = external(keeper, group.get("title"), group.get("artists"))
            target_isrc = keeper.get("isrc")
        else:
            kept = by_pointer.get(keeper.get("pointer"))
            if kept is None or kept in drop:
                stale.append({"group": label, "playlist_id": pid, "reason": "keeper not located"})
                continue
            target_uri, target_isrc = items[kept].uri, items[kept].isrc
        for isrc in isrcs - {target_isrc}:
            previous = keeper_of.get(isrc)
            if previous and previous[0] != target_isrc:
                held.append({"kind": "conflicting_keepers", "group": label, "isrc": isrc})
                continue
            keeper_of[isrc] = (target_isrc, target_uri, label)
        if target_isrc and (target_isrc in isrcs or keeper.get("action")):
            alias_target[target_isrc] = target_uri.split(":")[-1]
        if pid in frozen:
            skipped_frozen.append({"group": label, "playlist_id": pid, "occurrences": len(drop)})
            continue
        for i in drop:
            removals[pid][i] = f"{family}: {group.get('rule_applied', '')}"
        if keeper.get("action") == "add_target_then_remove_sources":
            inserts[pid].setdefault(min(drop), []).append(target_uri)

    for rep in decisions.get("unavailable_replacements", []):
        label = f"{rep.get('title')} / {', '.join(rep.get('artists') or [])}"
        target = spec.get("replacement_targets", {}).get(rep["isrc"]) or (
            rep.get("keeper") or {}
        ).get("spotify_id")
        if not target or not _status_ok(rep):
            held.append({"kind": "replacement", "group": label, "isrc": rep["isrc"]})
            continue
        uri = external(
            {**(rep.get("keeper") or {}), "spotify_id": target, "isrc": rep["isrc"]},
            rep.get("title"),
            rep.get("artists"),
        )
        alias_target[rep["isrc"]] = target
        for occurrence in rep["occurrences"]:
            pid = occurrence["playlist_id"]
            items = _items(live, pid)
            index = None if items is None else _locate(items, occurrence)
            if index is None:
                stale.append({"group": label, "playlist_id": pid, "reason": "occurrence moved"})
            elif pid in frozen:
                skipped_frozen.append({"group": label, "playlist_id": pid, "occurrences": 1})
            elif pid in curated or kinds.get(pid) == "curated":
                removals[pid][index] = "unavailable: same-ISRC playable replacement"
                inserts[pid].setdefault(index, []).append(uri)
    alias_target.update(spec.get("like_targets", {}))

    for kind in ("unavailable_exceptions", "flag_exceptions"):
        for entry in decisions.get(kind, []):
            exceptions.append(
                {
                    "kind": kind,
                    "isrc": entry.get("isrc"),
                    "spotify_ids": entry.get("spotify_ids"),
                    "title": entry.get("title"),
                    "artists": entry.get("artists"),
                    "rule": entry.get("rule_applied"),
                }
            )
    unavailable = {e["isrc"] for e in exceptions if e["kind"] == "unavailable_exceptions"}
    asked = set()
    for group in decisions.get("variant_groups", []):
        if group.get("status") == "ask":
            asked |= set(group["isrc"] if isinstance(group["isrc"], list) else [group["isrc"]])

    playlists = []
    after_items = {}
    for pid in sorted(set(removals) | set(inserts)):
        items = live.playlists[pid].items
        before = [it.uri for it in items]
        after = []
        for i, uri in enumerate(before):
            after.extend(inserts[pid].get(i, []))
            if i not in removals[pid]:
                after.append(uri)
        playlists.append(
            {
                "playlist_id": pid,
                "name": live.playlists[pid].name,
                "before": before,
                "after": after,
                "ops": edit_ops(before, after),
                "removed": [
                    {"index": i, "uri": before[i], "isrc": items[i].isrc, "reason": why}
                    for i, why in sorted(removals[pid].items())
                ],
                "inserted": [
                    {"index": i, "uri": u, "isrc": tracks.get(u, {}).get("isrc")}
                    for i, us in sorted(inserts[pid].items())
                    for u in us
                ],
            }
        )
        after_items[pid] = after

    liked = set(live.liked)
    observed_ids = defaultdict(dict)  # isrc -> track id -> playable
    members = set()
    for pid in curated:
        uris = after_items.get(pid) or [it.uri for it in live.playlists[pid].items or []]
        for uri in uris:
            meta = tracks.get(uri)
            if meta and meta["isrc"]:
                members.add((pid, meta["isrc"]))
                observed_ids[meta["isrc"]][meta["track_id"]] = meta["playable"]
    likes, unlikes = {}, []
    for isrc in sorted({i for _, i in members} - liked):
        if isrc in asked:
            held.append({"kind": "like_waits_for_recording_choice", "isrc": isrc})
        elif isrc in keeper_of:
            k_isrc, k_uri, why = keeper_of[isrc]
            if k_isrc not in liked:
                likes.setdefault(k_uri, {"isrc": k_isrc, "reason": f"album-first keeper: {why}"})
        elif isrc in unavailable:
            continue  # accepted exception: kept with original metadata, not liked
        else:
            ids = observed_ids[isrc]
            target = alias_target.get(isrc)
            if target is None:
                playable = sorted(t for t, ok in ids.items() if ok is not False)
                if len(ids) == 1 and playable:
                    target = playable[0]
                elif not playable:
                    held.append({"kind": "unavailable_without_decision", "isrc": isrc})
                    continue
                else:
                    held.append({"kind": "multiple_aliases", "isrc": isrc, "ids": sorted(ids)})
                    continue
            uri = f"spotify:track:{target}"
            if uri not in tracks:
                held.append({"kind": "like_target_not_observed", "isrc": isrc, "uri": uri})
                continue
            likes.setdefault(uri, {"isrc": isrc, "reason": "curated"})
    saved = defaultdict(set)  # isrc -> saved library URIs
    for row in live.raw.get("liked", []):
        item = mirror_mod.item_from_raw(row)
        if item.isrc:
            saved[item.isrc].add(f"spotify:track:{item.linked_from_id or item.track_id}")
    for isrc, (k_isrc, k_uri, why) in sorted(keeper_of.items()):
        if isrc in liked and isrc not in asked and k_isrc != isrc:
            if k_isrc not in liked:
                likes.setdefault(k_uri, {"isrc": k_isrc, "reason": f"album-first keeper: {why}"})
            for uri in sorted(saved[isrc]):
                unlikes.append({"uri": uri, "isrc": isrc, "keeper": k_uri, "reason": why})
    for isrc, target in sorted(alias_target.items()):
        keeper_uri = f"spotify:track:{target}"
        others = saved[isrc] - {keeper_uri}
        if isrc in liked and others and keeper_uri not in saved[isrc] and isrc not in asked:
            likes.setdefault(keeper_uri, {"isrc": isrc, "reason": "selected alias"})
            for uri in sorted(others):
                unlikes.append(
                    {"uri": uri, "isrc": isrc, "keeper": keeper_uri, "reason": "selected alias"}
                )

    names = {p.id: p.name for p in mirror.playlists.values()}
    package = {
        "version": VERSION,
        "observed_at": observed_at,
        "classify": [
            {"playlist_id": pid, "kind": kind, "name": live.playlists[pid].name}
            for pid, kind in sorted(classify.items())
            if pid in live.playlists
        ],
        "renames": [
            {
                "playlist_id": s["replaces"],
                "from": live.playlists[s["replaces"]].name,
                "to": s["name"] + suffix,
            }
            for s in spec["smart"]
            if s.get("replaces")
        ],
        "smart": [
            {"name": s["name"], "rule": s["rule"], "replaces": s.get("replaces")}
            for s in spec["smart"]
        ],
        "playlists": playlists,
        "likes": [{"uri": u, **v} for u, v in sorted(likes.items())],
        "unlikes": unlikes,
        "tracks": {
            u: tracks[u]
            for u in sorted(
                {u for u in likes} | {i["uri"] for p in playlists for i in p["inserted"]}
            )
        },
    }
    for s in spec["smart"]:
        rules.validate(s["rule"])
        if s.get("replaces") and s["replaces"] not in live.playlists:
            raise PackageError("smart destination replaces an unknown playlist")
    report = {
        "held": held,
        "stale": stale,
        "frozen_rollback_groups": skipped_frozen,
        "exceptions": exceptions,
        "playlist_names": names,
        "curated_unliked_candidates": len({i for _, i in members} - liked),
    }
    return {"package": package, "report": report}


# --- simulation -------------------------------------------------------------------


def _raw_track(meta: dict, uri: str) -> dict:
    return {
        "id": meta["track_id"],
        "uri": uri,
        "name": meta.get("name"),
        "is_local": False,
        "is_playable": meta.get("playable"),
        "external_ids": {"isrc": meta.get("isrc")},
        "duration_ms": meta.get("duration_ms"),
        "artists": [{"name": a} for a in meta.get("artists") or []],
        "album": {"name": meta.get("album"), "release_date": str(meta.get("album_year") or "")},
    }


def simulate(package: dict, mirror: Mirror, live, me_id: str, now: datetime):
    """(mirror, live) as they would be after `apply`, for a dry run. No I/O."""
    raw = dict(live.raw)
    raw["items"] = dict(raw["items"])
    stamp = _iso(now)
    by_uri = {}
    for body in live.raw["items"].values():
        for row in body:
            track = row.get("item") or row.get("track") or {}
            if track.get("uri"):
                by_uri.setdefault(track["uri"], track)
    for uri, meta in package["tracks"].items():
        by_uri.setdefault(uri, _raw_track(meta, uri))
    names = {r["playlist_id"]: r["to"] for r in package["renames"]}
    raw["playlists"] = [{**p, "name": names.get(p["id"], p["name"])} for p in live.raw["playlists"]]
    for edit in package["playlists"]:
        pid = edit["playlist_id"]
        original = {}
        for row in live.raw["items"][pid]:
            track = row.get("item") or row.get("track") or {}
            original.setdefault(track.get("uri"), []).append(row)
        rows = []
        for uri in edit["after"]:
            rows.append(
                original[uri].pop(0)
                if original.get(uri)
                else {"added_at": stamp, "item": by_uri[uri]}
            )
        raw["items"][pid] = rows
    mirror2 = copy.copy(mirror)
    mirror2.playlists = dict(mirror.playlists)
    for row in package["classify"]:
        mirror2.playlists[row["playlist_id"]] = Playlist(
            row["playlist_id"], row["name"], row["kind"], None, None, None, 1, None
        )
    for pid, name in names.items():
        if pid in mirror2.playlists:
            mirror2.playlists[pid] = copy.copy(mirror2.playlists[pid])
            mirror2.playlists[pid].name = name
    for smart in package["smart"]:
        pid = f"planned:{smart['name']}"
        raw["playlists"].append(
            {"id": pid, "name": smart["name"], "owner": {"id": me_id}, "snapshot_id": None}
        )
        raw["items"][pid] = []
        mirror2.playlists[pid] = Playlist(
            pid, smart["name"], "smart", smart["rule"], None, None, 1, None
        )
    unliked = {u["uri"] for u in package["unlikes"]}
    liked_rows = [
        row
        for row in live.raw["liked"]
        if f"spotify:track:{mirror_mod.item_from_raw(row).linked_from_id or mirror_mod.item_from_raw(row).track_id}"
        not in unliked
    ]
    for like in package["likes"]:
        liked_rows.insert(0, {"added_at": stamp, "track": by_uri[like["uri"]]})
    raw["liked"] = liked_rows
    return mirror2, mirror_mod.live_from_raw(raw, me_id, mirror2)


def adjust_curation(
    state: dict | None, package: dict, live_after, kinds: dict, unliked: set[str] = frozenset()
) -> dict:
    """Package likes are Music Sync's own; package unlikes are normalization, not gestures.

    Only recordings the package itself unliked leave the liked baseline, so a user's own
    unlike in the meantime still produces its review exception on the next run.
    """
    liked_isrcs = {like["isrc"] for like in package["likes"]} & set(live_after.liked)
    members = {
        (pid, item.isrc)
        for pid, playlist in live_after.playlists.items()
        if kinds.get(pid) == "curated"
        for item in playlist.items or []
        if item.isrc
    }
    if state is None:
        _, state = curation.advance(None, set(live_after.liked), members, "package")
    state = copy.deepcopy(state)
    baseline = state["baseline"]
    normalized = set(unliked) - set(live_after.liked)
    baseline["liked"] = sorted((set(baseline["liked"]) - normalized) | liked_isrcs)
    baseline["own_likes"] = sorted((set(baseline["own_likes"]) - normalized) | liked_isrcs)
    baseline["curated"] = [
        list(p) for p in sorted({tuple(p) for p in baseline["curated"]} | members)
    ]
    state["pending_likes"] = sorted(set(state.get("pending_likes", [])) - liked_isrcs)
    return state


# --- validation and apply ---------------------------------------------------------


def _uris(items) -> list[str]:
    return [it.uri for it in items]


def validate(package: dict, live, created: dict, attempted=()) -> list[str]:
    problems = []
    if package.get("version") != VERSION:
        problems.append("unsupported package version")
    for row in package["classify"] + package["renames"]:
        if row["playlist_id"] not in live.playlists:
            problems.append(f"{row['playlist_id']}: playlist missing or not owned")
    for row in package["renames"]:
        name = live.playlists.get(row["playlist_id"])
        if name and name.name not in (row["from"], row["to"]):
            problems.append(f"{row['playlist_id']}: renamed independently")
    replaced = {r["playlist_id"]: r for r in package["renames"]}
    for smart in package["smart"]:
        same = [
            pid
            for pid, p in live.playlists.items()
            if p.name == smart["name"]
            and pid != created.get(smart["name"])
            and not (pid == smart.get("replaces") and pid in replaced)
        ]
        if len(same) > (1 if smart["name"] in attempted and smart["name"] not in created else 0):
            problems.append(f"{smart['name']}: another owned playlist already has this name")
        if smart["name"] in created and created[smart["name"]] not in live.playlists:
            problems.append(f"{smart['name']}: created playlist is missing")
    for edit in package["playlists"]:
        items = _items(live, edit["playlist_id"])
        if items is None or progress(_uris(items), edit["before"], edit["ops"]) is None:
            problems.append(f"{edit['playlist_id']}: contents changed since the package was built")
    for like in package["likes"]:
        if like["uri"] not in package["tracks"]:
            problems.append(f"{like['uri']}: like target lacks track metadata")
    return problems


def _saved(spotify, market) -> dict[str, str]:
    """Saved library URI -> ISRC from a complete liked read."""
    out = {}
    for row in spotify.get_liked(market):
        item = mirror_mod.item_from_raw(row)
        out[f"spotify:track:{item.linked_from_id or item.track_id}"] = item.isrc
        out.setdefault(item.uri, item.isrc)
    return out


def apply(package, spotify, hub, settings, now, state, save) -> dict:
    """Serialized-worker execution. `state` is the retained checkpoint; `save` persists it."""
    market = settings.spotify_market
    me = spotify.me()["id"]
    m = mirror_mod.load_mirror(hub)
    live = mirror_mod.pull_live(spotify, market, me, m, full=True)
    if not state.get("backup"):
        key = archive.key_for(now)
        archive.put(settings, key, gzip.compress(json.dumps(live.raw).encode()))
        state["backup"] = key
        save()
    created = state.setdefault("created", {})
    problems = validate(package, live, created, state.get("attempted", []))
    if problems:
        return {"applied": False, "problems": problems, "backup": state["backup"]}
    today = now.date().isoformat()
    names = {p.id: p.name for p in m.playlists.values()}
    for row in package["classify"]:
        hub.push(
            "playlists",
            [
                {
                    "id": row["playlist_id"],
                    "name": live.playlists[row["playlist_id"]].name,
                    "kind": row["kind"],
                    "rule": None,
                    "pinned": 1,
                    "expires_at": None,
                    "deleted_at": None,
                }
            ],
        )
    for row in package["renames"]:
        if live.playlists[row["playlist_id"]].name != row["to"]:
            spotify.rename_playlist(row["playlist_id"], row["to"])
        hub.push("playlists", [{"id": row["playlist_id"], "name": row["to"]}])
        names[row["playlist_id"]] = row["to"]
    attempted = state.setdefault("attempted", [])
    for smart in package["smart"]:
        name = smart["name"]
        if name not in created and name in attempted:
            known = set(m.playlists) | {r["playlist_id"] for r in package["renames"]}
            fresh = [p["id"] for p in spotify.get_playlists() if p.get("name") == name]
            fresh = [pid for pid in fresh if pid not in known]
            if len(fresh) > 1:
                raise PackageError(f"{name}: several new playlists after an uncertain create")
            if fresh:
                created[name] = fresh[0]
                save()
        if name not in created:
            attempted.append(name)
            save()  # an uncertain create is adopted by name, never repeated blindly
            text = rules.describe(smart["rule"], today, names)
            body = spotify.create_playlist(name, text, public=False)
            created[name] = body["id"]
            save()
        hub.push(
            "playlists",
            [
                {
                    "id": created[name],
                    "name": name,
                    "kind": "smart",
                    "rule": smart["rule"],
                    "description": rules.describe(smart["rule"], today, names),
                    "pinned": 1,
                    "expires_at": None,
                    "deleted_at": None,
                }
            ],
        )
    edited = 0
    for edit in package["playlists"]:
        pid = edit["playlist_id"]
        current = _uris(
            mirror_mod.item_from_raw(r) for r in spotify.get_playlist_items(pid, market)
        )
        done = progress(current, edit["before"], edit["ops"])
        if done is None:
            raise PackageError(f"{pid}: contents changed during the package run")
        for op in edit["ops"][done:]:
            if op["op"] == "delete":
                spotify.remove_items(pid, op["uris"])
            else:
                spotify.add_items(pid, op["uris"], position=op["position"])
            edited += 1
    saved = _saved(spotify, market)
    to_like = [like["uri"] for like in package["likes"] if like["uri"] not in saved]
    if to_like:
        state["likes_attempted"] = True
        save()
        spotify.like(to_like)
        saved = _saved(spotify, market)
    missing = [u for u in (like["uri"] for like in package["likes"]) if u not in saved]
    keepers = set(saved)
    to_unlike = [
        u["uri"] for u in package["unlikes"] if u["keeper"] in keepers and u["uri"] in saved
    ]
    held = [u["uri"] for u in package["unlikes"] if u["keeper"] not in keepers]
    if to_unlike:
        spotify.unlike(to_unlike)
    after = mirror_mod.pull_live(spotify, market, me, m, full=True)
    key = archive.key_for(datetime.now(timezone.utc))
    archive.put(settings, key, gzip.compress(json.dumps(after.raw).encode()))
    saved_after = {
        f"spotify:track:{mirror_mod.item_from_raw(r).linked_from_id or mirror_mod.item_from_raw(r).track_id}"
        for r in after.raw["liked"]
    } | {mirror_mod.item_from_raw(r).uri for r in after.raw["liked"]}
    mismatches = [
        e["playlist_id"]
        for e in package["playlists"]
        if _uris(after.playlists[e["playlist_id"]].items or []) != e["after"]
    ]
    for row in package["renames"]:
        pid = row["playlist_id"]
        if after.playlists[pid].name != row["to"]:
            mismatches.append(pid)
        if _uris(after.playlists[pid].items or []) != _uris(live.playlists[pid].items or []):
            mismatches.append(f"{pid}: rollback copy contents changed")
    mismatches += [name for name, pid in created.items() if pid not in after.playlists]
    still_liked = [u for u in to_unlike if u in saved_after]
    receipt = {
        "applied": True,
        "backup": state["backup"],
        "readback": key,
        "created": dict(created),
        "edit_ops": edited,
        "liked": len(to_like),
        "likes_missing": missing,
        "unliked": len(to_unlike),
        "unlikes_held": held,
        "unlikes_still_present": still_liked,
        "mismatches": mismatches,
        "verified": not (missing or still_liked or mismatches),
    }
    kinds = {pid: p.kind for pid, p in m.playlists.items()}
    kinds.update({r["playlist_id"]: r["kind"] for r in package["classify"]})
    retained = archive.get(settings, curation.state_key(settings.workspace))
    previous = json.loads(gzip.decompress(retained)) if retained else None
    unliked_isrcs = {u["isrc"] for u in package["unlikes"] if u["uri"] in to_unlike}
    adjusted = adjust_curation(previous, package, after, kinds, unliked_isrcs)
    archive.put(
        settings,
        curation.state_key(settings.workspace),
        gzip.compress(json.dumps(adjusted).encode()),
    )
    return receipt
