"""Load the life-data mirror and pull live Spotify state into plain dataclasses."""

import json
import re

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
    "observed_at",
    "detail",
]


def _j(v, default):
    if v in (None, ""):
        return default
    return json.loads(v) if isinstance(v, str) else v


def load_mirror(hub) -> Mirror:
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
        for r in hub.pull("songs", SONG_COLS)
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
        for r in hub.pull("playlists", PLAYLIST_COLS)
        if not r.get("deleted_at")
    }
    memberships, deleted = {}, []
    for r in hub.pull("playlist_songs", MEMBER_COLS):
        m = Membership(
            r["playlist_id"], r["isrc"], r["spotify_track_id"], r["added_at"], r.get("deleted_at")
        )
        (deleted.append(m) if m.deleted_at else memberships.__setitem__((m.playlist_id, m.isrc), m))
    provenance = hub.pull("provenance", PROV_COLS)
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
    return Mirror(songs, playlists, memberships, deleted, captures, observations)


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


def pull_live(spotify, market: str, me_id: str, mirror: Mirror, full: bool = False) -> Live:
    raw = {"playlists": [], "items": {}, "liked": []}
    playlists = {}
    for p in spotify.get_playlists():
        raw["playlists"].append(p)
        if (p.get("owner") or {}).get("id") != me_id:
            continue
        known = mirror.playlists.get(p["id"])
        # smart playlists get rewritten by rule materialization without moving their snapshot
        # (a heart/un-heart elsewhere never bumps them), so always re-fetch those
        if (
            not full
            and known
            and known.kind != "smart"
            and known.snapshot_id
            and known.snapshot_id == p.get("snapshot_id")
        ):
            items = None
        else:
            body = spotify.get_playlist_items(p["id"], market)
            raw["items"][p["id"]] = body
            items = [item_from_raw(i) for i in body]
        playlists[p["id"]] = LivePlaylist(
            p["id"], p["name"], p.get("description"), p.get("snapshot_id"), items
        )
    liked_raw = spotify.get_liked(market)
    raw["liked"] = liked_raw
    liked = {}
    observations = []
    for i in liked_raw:
        it = item_from_raw(i)
        observations.append(it)
        if it.isrc and it.isrc not in liked:
            liked[it.isrc] = it
    return Live(playlists, liked, raw, observations)
