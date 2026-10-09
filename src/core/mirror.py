"""Load the life-data mirror and pull live Spotify state into plain dataclasses."""

import json
import re
from collections import defaultdict

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


def _j(v, default):
    if v in (None, ""):
        return default
    return json.loads(v) if isinstance(v, str) else v


def load_mirror(hub) -> Mirror:
    revisions = {}

    def pull(table, columns, **kwargs):
        rows = hub.pull(table, list(dict.fromkeys([*columns, "updated_at", "hub_at"])), **kwargs)
        revisions.setdefault(table, {}).update(
            {row["id"]: {key: row.get(key) for key in ("updated_at", "hub_at")} for row in rows}
        )
        return rows

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
        for r in pull("songs", SONG_COLS)
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
        for r in pull("playlists", PLAYLIST_COLS)
        if not r.get("deleted_at")
    }
    memberships, deleted = {}, []
    for r in pull("playlist_songs", MEMBER_COLS):
        m = Membership(
            r["playlist_id"], r["isrc"], r["spotify_track_id"], r["added_at"], r.get("deleted_at")
        )
        (deleted.append(m) if m.deleted_at else memberships.__setitem__((m.playlist_id, m.isrc), m))
    # Provenance holds every media source; pull only the two song slices this
    # mirror reads, or the hub cannot serve the table within its limits.
    provenance = pull(
        "provenance", PROV_COLS, where={"to_kind": "songs", "rel": "imported_from"}
    ) + pull(
        "provenance",
        PROV_COLS,
        where={"to_kind": "songs", "rel": "evidence_of", "asserted_by": "music-sync"},
    )
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


def pull_live(spotify, market: str, me_id: str, mirror: Mirror, full: bool = False) -> Live:
    raw = {"playlists": [], "items": {}, "liked": []}
    for p in spotify.get_playlists():
        raw["playlists"].append(p)
        if (p.get("owner") or {}).get("id") != me_id:
            continue
        known = mirror.playlists.get(p["id"])
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


def live_from_raw(raw: dict, me_id: str, mirror: Mirror) -> Live:
    """Interpret one retained observation; owned playlists without items were skipped."""
    playlists = {}
    for p in raw["playlists"]:
        if (p.get("owner") or {}).get("id") != me_id:
            continue
        body = raw["items"].get(p["id"])
        items = None if body is None else [item_from_raw(i) for i in body]
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
