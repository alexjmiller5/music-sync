"""Load the soma mirror and pull live Spotify state into plain dataclasses."""

import gzip
import hashlib
import json
import re
import time

import httpx
from collections import defaultdict

from core import table_pull
from core.model import ISRC_RE, Live, LiveItem, LivePlaylist, Membership, Mirror, Playlist, Song

SONG_COLS = [
    "id",
    "liked",
    "liked_at",
    "first_seen",
    "spotify_ids",
    "spotify_playable",
    "first_year",
    "deezer_genres",
    "mb_tags",
    "title",
    "artists",
    "album",
    "album_year",
    "duration_ms",
    "shazam_last_at",
    "found_at",
    "resolves_capture",
    "deleted_at",
]
PLAYLIST_COLS = [
    "id",
    "name",
    "kind",
    "rule",
    "description",
    "snapshot_id",
    "pinned",
    "expires_at",
    "cover_file",
    "cover_sha256",
    "cover_applied",
    "deleted_at",
]
MEMBER_COLS = ["id", "playlist_id", "isrc", "spotify_track_id", "added_at", "deleted_at"]
PROV_COLS = [
    "id",
    "from_kind",
    "from_ref",
    "to_kind",
    "to_ref",
    "rel",
    "deleted_at",
    "asserted_by",
    "detail",
]


CACHE_VERSION = 2
FULL_REFRESH_SECONDS = 7 * 24 * 3600
# Provenance holds every media source; the mirror reads only these two song
# slices, or the hub cannot serve the table within its limits.
PROVENANCE_SLICES = (
    {"to_kind": "songs", "rel": "imported_from"},
    {"to_kind": "songs", "rel": "evidence_of", "asserted_by": "music-sync"},
)


def new_cache(now: float | None = None) -> dict:
    return {
        "version": CACHE_VERSION,
        "refreshed_at": time.time() if now is None else now,
        "pull": None,
        "tables": {},
        "slices": {},
    }


def cache_key(workspace: str) -> str:
    return "music-sync/mirror-cache/" + hashlib.sha256(workspace.encode()).hexdigest() + ".json.gz"


def load_cache(settings) -> dict:
    """The incremental mirror cache from the recovery bucket, or a fresh one."""
    from core import archive

    raw = archive.get(settings, cache_key(settings.workspace))
    cache = json.loads(gzip.decompress(raw)) if raw else None
    return cache if cache and cache.get("version") == CACHE_VERSION else new_cache()


def save_cache(settings, cache: dict) -> None:
    from core import archive

    archive.put(settings, cache_key(settings.workspace), gzip.compress(json.dumps(cache).encode()))


def _j(v, default):
    if v in (None, ""):
        return default
    return json.loads(v) if isinstance(v, str) else v


def _playlist(r: dict) -> Playlist:
    return Playlist(
        r["id"],
        r["name"],
        r["kind"],
        _j(r["rule"], None),
        r["description"],
        r["snapshot_id"],
        int(r["pinned"] or 0),
        r["expires_at"],
        r.get("cover_file"),
        r.get("cover_sha256"),
        _j(r.get("cover_applied"), None),
    )


def load_playlists(hub) -> Mirror:
    """Playlists only: enough for kind/rule configuration, without songs or provenance."""
    rows = hub.pull("playlists", [*PLAYLIST_COLS, "updated_at", "hub_at"])
    playlists = {r["id"]: _playlist(r) for r in rows if not r.get("deleted_at")}
    return Mirror({}, playlists, {}, [], set())


def load_mirror(
    hub, cache: dict | None = None, now: float | None = None, checkpoint=None
) -> Mirror:
    """Full pulls, or with `cache` only what the hub received since the last load.

    Songs, playlists and memberships go through soma's table-pull helper: one
    `/v1/cursor` request per quiet round, and the cursor is the mark read BEFORE
    pulling, so a row committed mid-walk is read next round. Provenance is
    watched, never pulled whole: a slice is re-read from its held mark only when
    the table's mark moved. Soft deletes arrive as rows. A cache older than a
    week is rebuilt from scratch (purges never appear in deltas). `checkpoint`
    persists the cache after each step so a cold load survives a timeout.
    """
    now = time.time() if now is None else now
    if cache is not None and now - cache.get("refreshed_at", 0) > FULL_REFRESH_SECONDS:
        cache.clear()
        cache.update(new_cache(now))
    columns = {
        table: list(dict.fromkeys([*cols, "updated_at", "hub_at"]))
        for table, cols in (
            ("songs", SONG_COLS),
            ("playlists", PLAYLIST_COLS),
            ("playlist_songs", MEMBER_COLS),
        )
    }
    prov_columns = list(dict.fromkeys([*PROV_COLS, "updated_at", "hub_at"]))
    if cache is None:
        rows = {table: hub.pull(table, cols) for table, cols in columns.items()}
        provenance = [
            r
            for where in PROVENANCE_SLICES
            for r in hub.pull("provenance", prov_columns, where=where)
        ]
    else:
        out = table_pull.pull_tables(
            hub.base, None, columns, cache["pull"], watch=("provenance",), post=hub.read
        )
        for table, change in out["changes"].items():
            held = cache["tables"].setdefault(table, {})
            if change["full"]:
                held.clear()
            held.update({r["id"]: r for r in [*change["rows"], *change["tombstones"]]})
        cache["pull"] = out["state"]
        if checkpoint:
            checkpoint()
        watched = out["watched"]["provenance"]
        provenance = []
        for where in PROVENANCE_SLICES:
            key = json.dumps([prov_columns, where], sort_keys=True)
            slot = cache["slices"].setdefault(key, {"mark": None, "n": None, "rows": {}})
            if (slot["mark"], slot["n"]) != (watched["mark"], watched["n"]):
                # A mark behind the held one (a restore or purge) re-reads the slice whole.
                if slot["mark"] is None or watched["mark"] < slot["mark"]:
                    slot["rows"], since = {}, ""
                else:
                    since = slot["mark"]
                delta = hub.pull("provenance", prov_columns, since=since, where=where)
                slot["rows"].update({r["id"]: r for r in delta})
                slot["mark"], slot["n"] = watched["mark"], watched["n"]
                if checkpoint:
                    checkpoint()
            provenance += slot["rows"].values()
        rows = {table: list(cache["tables"][table].values()) for table in columns}
    return _from_rows(rows, provenance)


def _from_rows(rows, provenance):
    revisions = {}
    for table, got in [*rows.items(), ("provenance", provenance)]:
        revisions.setdefault(table, {}).update(
            {row["id"]: {key: row.get(key) for key in ("updated_at", "hub_at")} for row in got}
        )

    songs = {
        r["id"]: Song(
            r["id"],
            int(r["liked"] or 0),
            r["liked_at"],
            r["first_seen"],
            _j(r["spotify_ids"], []),
            r["spotify_playable"],
            r["first_year"],
            _j(r["deezer_genres"], []),
            _j(r["mb_tags"], []),
            r["title"],
            _j(r["artists"], []),
            r["album"],
            r["album_year"],
            r["duration_ms"],
            r.get("shazam_last_at"),
            r.get("found_at"),
            r.get("resolves_capture"),
        )
        for r in rows["songs"]
        if not r.get("deleted_at")
    }
    playlists = {r["id"]: _playlist(r) for r in rows["playlists"] if not r.get("deleted_at")}
    memberships, deleted = {}, []
    for r in rows["playlist_songs"]:
        m = Membership(
            r["playlist_id"], r["isrc"], r["spotify_track_id"], r["added_at"], r.get("deleted_at")
        )
        (deleted.append(m) if m.deleted_at else memberships.__setitem__((m.playlist_id, m.isrc), m))
    captures = {
        (r["to_ref"], r["from_kind"])
        for r in provenance
        if r["to_kind"] == "songs" and r["rel"] == "imported_from" and not r.get("deleted_at")
    }
    observations = []
    for r in provenance:
        detail = _j(r.get("detail"), {})
        if (
            r["to_kind"] == "songs"
            and r["from_kind"] == "takeout"
            and r["rel"] == "evidence_of"
            and r.get("asserted_by") == "music-sync"
            and detail.get("kind") == "spotify_observation"
            and not r.get("deleted_at")
        ):
            observations.append({**r, "detail": detail})
    return Mirror(songs, playlists, memberships, deleted, captures, observations, revisions)


def capture_playlists(hub) -> list[dict]:
    """Only inbox/curated identities are needed for capture ordering."""
    return hub.query(
        "playlists", PLAYLIST_COLS, {"column": "kind", "op": "in", "value": ["inbox", "curated"]}
    )


def capture_mirror(hub, playlists: list[dict], isrcs: set[str]) -> Mirror:
    """Fresh context for the live inbox and selected recording, independent of cache age."""
    rows = {"playlists": playlists, "songs": [], "playlist_songs": []}
    evidence = []
    ids = sorted(isrcs)
    for start in range(0, len(ids), 200):
        chunk = ids[start : start + 200]
        for table, columns, key in (
            ("songs", SONG_COLS, "id"),
            ("playlist_songs", MEMBER_COLS, "isrc"),
        ):
            rows[table].extend(
                hub.query(table, columns, {"column": key, "op": "in", "value": chunk})
            )
        evidence.extend(
            hub.pull_many(
                "provenance",
                PROV_COLS,
                "to_ref",
                chunk,
                {
                    "to_kind": "songs",
                    "rel": "evidence_of",
                    "asserted_by": "music-sync",
                    "from_kind": "takeout",
                },
            )
        )
    return _from_rows(rows, evidence)


def item_from_raw(raw: dict) -> LiveItem:
    t = raw.get("item") or raw.get("track") or {}
    isrc = ((t.get("external_ids") or {}).get("isrc") or "").strip().upper()
    album = t.get("album") or {}
    year = (album.get("release_date") or "").split("-")[0]
    return LiveItem(
        isrc=isrc if re.match(ISRC_RE, isrc) else None,
        track_id=t.get("id"),
        uri=t.get("uri"),
        added_at=raw.get("added_at") or "",
        playable=t.get("is_playable") if isinstance(t.get("is_playable"), bool) else None,
        is_local=bool(t.get("is_local")),
        name=t.get("name"),
        artists=[a.get("name") for a in t.get("artists") or [] if a.get("name")],
        album=album.get("name"),
        album_type=album.get("album_type"),
        release_date=album.get("release_date"),
        release_date_precision=album.get("release_date_precision"),
        album_year=int(year) if re.fullmatch(r"[0-9]{4}", year) and int(year) else None,
        duration_ms=t.get("duration_ms"),
        linked_from_id=(t.get("linked_from") or {}).get("id"),
    )


def validate_recording_aliases(items: list[LiveItem]) -> None:
    recordings = {}
    for item in items:
        if item.uri and item.isrc:
            previous = recordings.setdefault(item.uri, item.isrc)
            if previous != item.isrc:
                raise ValueError(
                    "Spotify URI has conflicting recording identities; review required"
                )


def pull_live(
    spotify,
    market: str,
    me_id: str,
    mirror: Mirror,
    full: bool = False,
    smart_orders: dict[str, list[str]] | None = None,
    availability_audit: bool = False,
    preserve_unavailable: bool = True,
    previous_holds: list | None = None,
    previous_likes: list | None = None,
) -> Live:
    """`smart_orders`: the order (URIs) Music Sync left each smart playlist in. A smart
    playlist whose Spotify snapshot equals the one the catalog recorded after Music
    Sync's own last write, and whose recorded order holds exactly its catalog membership,
    is not read again: its items come from the catalog membership in that order (listed
    under raw["reused"] and raw["reused_order"]). Anything else re-reads it."""
    raw = {"playlists": [], "items": {}, "liked": []}
    for p in spotify.get_playlists():
        raw["playlists"].append(p)
        if (p.get("owner") or {}).get("id") != me_id:
            continue
        known = mirror.playlists.get(p["id"])
        order = (smart_orders or {}).get(p["id"])
        if (
            not availability_audit
            and order is not None
            and known
            and known.kind == "smart"
            and known.snapshot_id
            and known.snapshot_id == p.get("snapshot_id")
            and sorted(order) == sorted(it.uri for it in _catalog_items(mirror, p["id"]))
        ):
            raw.setdefault("reused", {})[p["id"]] = p["snapshot_id"]
            raw.setdefault("reused_order", {})[p["id"]] = order
            continue
        # smart playlists get rewritten by rule materialization without moving their snapshot
        # (a heart/un-heart elsewhere never bumps them), so always re-fetch those; the
        # inbox's order is planned against what it holds now, so it is always read too
        if (
            full
            or not known
            or known.kind in ("smart", "inbox")
            or not known.snapshot_id
            or known.snapshot_id != p.get("snapshot_id")
        ):
            raw["items"][p["id"]] = spotify.get_playlist_items(p["id"], market)
    raw["liked"] = spotify.get_liked(market)
    live = live_from_raw(raw, me_id, mirror)
    if preserve_unavailable:
        _check_missing(spotify, market, mirror, live, previous_holds or [], previous_likes or [])
    if availability_audit:
        from core import metadata

        unavailable = {isrc for _, isrc in live.raw.get("availability_holds", [])}
        unavailable.update(
            it.isrc
            for p in live.playlists.values()
            for it in p.items or []
            if it.isrc and it.playable is False
        )
        candidates = []
        for isrc in sorted(unavailable):
            song = mirror.songs.get(isrc)
            if song is None:
                continue
            results = [
                item_from_raw({"item": body, "added_at": ""})
                for body in spotify.search_isrc(isrc, market)
            ]
            selected = metadata.replacement(song, results)
            if selected:
                body = spotify.get_track(selected.track_id, market)
                verified = item_from_raw({"item": body, "added_at": ""})
                if metadata.replacement(song, [verified]):
                    candidates.append({"item": body, "added_at": ""})
        live.raw["replacement_candidates"] = candidates
    return live_from_raw(raw, me_id, mirror)


def _check_missing(spotify, market, mirror, live, previous_holds, previous_likes):
    """A missing item is a user removal only when its identity is freshly playable."""
    checks = {}
    held = []
    liked = []

    def playable(isrc, track_id):
        if not track_id:
            return False
        if track_id not in checks:
            try:
                body = spotify.get_track(track_id, market)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise  # Retry-After and service failures abort the whole observation.
                body = {
                    "id": track_id,
                    "uri": f"spotify:track:{track_id}",
                    "external_ids": {"isrc": isrc},
                    "is_playable": False,
                }
            item = item_from_raw({"item": body, "added_at": ""})
            if item.isrc and item.isrc != isrc:
                raise ValueError("Missing recording identity changed; review required")
            checks[track_id] = {"item": body, "added_at": ""}
        it = item_from_raw(checks[track_id])
        return it.isrc == isrc and it.playable is True

    for (pid, isrc), member in mirror.memberships.items():
        playlist = live.playlists.get(pid)
        known = mirror.playlists.get(pid)
        if not playlist or playlist.items is None or (known and known.kind == "archive"):
            continue
        if any(it.isrc == isrc for it in playlist.items):
            continue
        confirmed = playable(isrc, member.spotify_track_id)
        if (
            [pid, isrc] in previous_holds
            or not confirmed
            or any(not it.isrc for it in playlist.items)
        ):
            held.append([pid, isrc])
    for isrc, song in mirror.songs.items():
        if song.liked and isrc not in live.liked:
            confirmed = bool(song.spotify_ids) and any(
                playable(isrc, tid) for tid in song.spotify_ids
            )
            if isrc in previous_likes or not confirmed:
                liked.append(isrc)
    if checks or held or liked:
        live.raw["availability_checks"] = list(checks.values())
        live.raw["availability_holds"] = held
        live.raw["availability_likes"] = liked


def _catalog_items(mirror: Mirror, pid: str) -> list[LiveItem]:
    """A reused smart playlist's contents as the catalog recorded them."""
    return [
        LiveItem(
            isrc,
            m.spotify_track_id,
            f"spotify:track:{m.spotify_track_id}",
            m.added_at,
            None,
            False,
            None,
            [],
            observed=False,
        )
        for (playlist_id, isrc), m in sorted(mirror.memberships.items())
        if playlist_id == pid
    ]


def live_from_raw(raw: dict, me_id: str, mirror: Mirror) -> Live:
    """Interpret one retained observation; owned playlists without items were skipped."""
    playlists = {}
    for p in raw["playlists"]:
        if (p.get("owner") or {}).get("id") != me_id:
            continue
        body = raw["items"].get(p["id"])
        items = None if body is None else [item_from_raw(i) for i in body]
        if body is None and p["id"] in raw.get("reused", {}):
            rank = {uri: i for i, uri in enumerate(raw.get("reused_order", {}).get(p["id"], []))}
            items = sorted(_catalog_items(mirror, p["id"]), key=lambda it: rank.get(it.uri, -1))
        playlists[p["id"]] = LivePlaylist(
            p["id"],
            p["name"],
            p.get("description"),
            p.get("snapshot_id"),
            items,
            ((p.get("images") or [{}])[0]).get("url"),
        )
    liked = {}
    observations = []
    known_aliases = defaultdict(set)
    for song in mirror.songs.values():
        for track_id in song.spotify_ids:
            known_aliases[track_id].add(song.id)
    for i in raw["liked"]:
        it = item_from_raw(i)
        known = known_aliases.get(it.track_id, set()) | known_aliases.get(it.linked_from_id, set())
        if known and known != {it.isrc}:
            raise ValueError("Liked recording identity changed or missing; review required")
        observations.append(it)
        if it.isrc and it.isrc not in liked:
            liked[it.isrc] = it
    observations.extend(item_from_raw(row) for row in raw.get("availability_checks", []))
    observations.extend(item_from_raw(row) for row in raw.get("replacement_candidates", []))
    for isrc in raw.get("availability_likes", []):
        song = mirror.songs.get(isrc)
        if song and song.liked:
            liked.setdefault(
                isrc,
                LiveItem(
                    isrc,
                    None,
                    None,
                    song.liked_at or song.first_seen,
                    None,
                    False,
                    None,
                    [],
                    observed=False,
                ),
            )
    validate_recording_aliases(
        [*observations, *(item for p in playlists.values() for item in p.items or [])]
    )
    return Live(playlists, liked, raw, observations)
