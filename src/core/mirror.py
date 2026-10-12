"""Load the soma mirror and pull live Spotify state into plain dataclasses."""

import gzip
import hashlib
import json
import re
import time
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


def load_playlists(hub) -> Mirror:
    """Playlists only: enough for kind/rule configuration, without songs or provenance."""
    rows = hub.pull("playlists", [*PLAYLIST_COLS, "updated_at", "hub_at"])
    playlists = {
        r["id"]: Playlist(
            r["id"],
            r["name"],
            r["kind"],
            _j(r["rule"], None),
            r["description"],
            r["snapshot_id"],
            int(r["pinned"] or 0),
            r["expires_at"],
        )
        for r in rows
        if not r.get("deleted_at")
    }
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
    revisions = {}
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
        )
        for r in rows["songs"]
        if not r.get("deleted_at")
    }
    playlists = {
        r["id"]: Playlist(
            r["id"],
            r["name"],
            r["kind"],
            _j(r["rule"], None),
            r["description"],
            r["snapshot_id"],
            int(r["pinned"] or 0),
            r["expires_at"],
        )
        for r in rows["playlists"]
        if not r.get("deleted_at")
    }
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
    spotify, market: str, me_id: str, mirror: Mirror, full: bool = False, reuse_smart: bool = False
) -> Live:
    """`reuse_smart`: a smart playlist whose Spotify snapshot equals the one the catalog
    recorded after Music Sync's own last write is not read again; its items come from
    the catalog membership (listed under raw["reused"]). Any other change re-reads it."""
    raw = {"playlists": [], "items": {}, "liked": []}
    for p in spotify.get_playlists():
        raw["playlists"].append(p)
        if (p.get("owner") or {}).get("id") != me_id:
            continue
        known = mirror.playlists.get(p["id"])
        if (
            reuse_smart
            and known
            and known.kind == "smart"
            and known.snapshot_id
            and known.snapshot_id == p.get("snapshot_id")
        ):
            raw.setdefault("reused", {})[p["id"]] = p["snapshot_id"]
            continue
        # smart playlists get rewritten by rule materialization without moving their snapshot
        # (a heart/un-heart elsewhere never bumps them), so always re-fetch those
        if (
            full
            or not known
            or known.kind == "smart"
            or not known.snapshot_id
            or known.snapshot_id != p.get("snapshot_id")
        ):
            raw["items"][p["id"]] = spotify.get_playlist_items(p["id"], market)
    raw["liked"] = spotify.get_liked(market)
    return live_from_raw(raw, me_id, mirror)


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
            items = _catalog_items(mirror, p["id"])
        playlists[p["id"]] = LivePlaylist(
            p["id"], p["name"], p.get("description"), p.get("snapshot_id"), items
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
    validate_recording_aliases(
        [*observations, *(item for p in playlists.values() for item in p.items or [])]
    )
    return Live(playlists, liked, raw, observations)
