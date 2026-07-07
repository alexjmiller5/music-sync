"""Idempotent sync of exported playlists into the sandbox Notion DBs.

Plain Python (no Modal imports). Upserts are keyed on Spotify URI for songs
(fallback artist|name) and on Name for playlists — the GDPR export carries no
playlist spotify id. Reruns create zero duplicates.
"""

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
import structlog

from core.models import FollowedArtist, Playlist, Song

log = structlog.get_logger()

API = "https://api.notion.com"
NOTION_VERSION = "2026-03-11"
MIN_INTERVAL = 0.34  # ~3 req/s, Notion's documented average rate limit


class NotionClient:
    def __init__(self, token: str, http: httpx.Client | None = None):
        self._http = http or httpx.Client(base_url=API, timeout=30)
        self._http.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
            }
        )
        self._last_request = 0.0

    def request(self, method: str, path: str, json: dict | None = None) -> dict:
        transient_failures = 0
        while True:
            time.sleep(max(0.0, MIN_INTERVAL - (time.monotonic() - self._last_request)))
            self._last_request = time.monotonic()
            try:
                resp = self._http.request(method, path, json=json)
            except httpx.TransportError as exc:  # timeouts, resets — safe to retry (upsert)
                resp = None
                reason = repr(exc)
            if resp is not None:
                if resp.status_code == 429:
                    wait = float(resp.headers.get("Retry-After", "1"))
                    log.warning("rate_limited", retry_after=wait)
                    time.sleep(wait)
                    continue
                if resp.status_code < 500:
                    resp.raise_for_status()
                    return resp.json()
                reason = f"HTTP {resp.status_code}"
            transient_failures += 1
            if transient_failures > 3:
                if resp is not None:
                    resp.raise_for_status()
                raise httpx.TransportError(f"giving up after retries: {reason}")
            log.warning("transient_error_retrying", reason=reason, attempt=transient_failures)
            time.sleep(2**transient_failures)

    def query_all(self, data_source_id: str) -> list[dict]:
        results, cursor = [], None
        while True:
            body = self.request(
                "POST",
                f"/v1/data_sources/{data_source_id}/query",
                json={"page_size": 100, "start_cursor": cursor} if cursor else {"page_size": 100},
            )
            results.extend(body["results"])
            if not body.get("has_more"):
                return results
            cursor = body["next_cursor"]


# --- keys ---------------------------------------------------------------------


def song_key(song: Song) -> str:
    return song.spotify_uri or f"{song.artist}|{song.name}".lower()


def _plain(prop: dict) -> str:
    parts = prop.get("title") or prop.get("rich_text") or []
    return "".join(p["plain_text"] for p in parts)


def _page_song_key(page: dict) -> str:
    props = page["properties"]
    uri = _plain(props["Spotify URI"])
    return uri or f"{_plain(props['Artist'])}|{_plain(props['Name'])}".lower()


# --- existing-page indexes ----------------------------------------------------


@dataclass
class SongPage:
    page_id: str
    relation_ids: set[str]
    relations_complete: bool  # False when Notion truncated the relation list


def fetch_song_index(client: NotionClient, data_source_id: str) -> dict[str, SongPage]:
    index = {}
    for pg in client.query_all(data_source_id):
        rel = pg["properties"]["Playlists"]
        index[_page_song_key(pg)] = SongPage(
            page_id=pg["id"],
            relation_ids={r["id"] for r in rel.get("relation", [])},
            relations_complete=not rel.get("has_more", False),
        )
    return index


def fetch_playlist_index(client: NotionClient, data_source_id: str) -> dict[str, str]:
    return {_plain(pg["properties"]["Name"]): pg["id"] for pg in client.query_all(data_source_id)}


# --- property builders ----------------------------------------------------------


def _rt(text: str | None) -> dict:
    return {"rich_text": [{"text": {"content": text[:2000]}}] if text else []}


def _playlist_props(pl: Playlist, now_iso: str) -> dict:
    return {
        "Name": {"title": [{"text": {"content": pl.name}}]},
        "Description": _rt(pl.description),
        "Track Count": {"number": len(pl.songs)},
        # Spotify ID: the GDPR export has no playlist ids; live API sync fills this in later
        "Last Synced": {"date": {"start": now_iso}},
    }


def _song_props(song: Song, playlist_page_ids: set[str]) -> dict:
    return {
        "Name": {"title": [{"text": {"content": song.name[:2000]}}]},
        "Artist": _rt(song.artist),
        "Album": _rt(song.album),
        "Spotify URI": _rt(song.spotify_uri),
        "Added": {"date": {"start": song.added_date.isoformat()}},
        "Playlists": {"relation": [{"id": i} for i in sorted(playlist_page_ids)]},
    }


# --- sync ---------------------------------------------------------------------


@dataclass
class _SongAgg:
    song: Song
    playlist_page_ids: set[str] = field(default_factory=set)


def sync_snapshot(client: NotionClient, cfg: dict, playlists: list[Playlist]) -> dict:
    """Upsert playlists then songs; one song page per unique song_key."""
    now_iso = datetime.now(UTC).isoformat()
    stats = {"playlists_created": 0, "playlists_updated": 0, "songs_created": 0, "songs_updated": 0}

    pl_index = fetch_playlist_index(client, cfg["playlists_data_source_id"])
    pl_page_ids: dict[str, str] = {}
    for pl in playlists:
        props = _playlist_props(pl, now_iso)
        if pl.name in pl_index:
            page_id = pl_index[pl.name]
            client.request("PATCH", f"/v1/pages/{page_id}", json={"properties": props})
            stats["playlists_updated"] += 1
        else:
            page = client.request(
                "POST",
                "/v1/pages",
                json={
                    "parent": {
                        "type": "data_source_id",
                        "data_source_id": cfg["playlists_data_source_id"],
                    },
                    "properties": props,
                },
            )
            page_id = page["id"]
            stats["playlists_created"] += 1
        pl_page_ids[pl.name] = page_id
        log.info("playlist_synced", name=pl.name, tracks=len(pl.songs))

    # dedupe: one Songs page per key, union of playlist relations (first-seen song wins fields)
    agg: dict[str, _SongAgg] = {}
    for pl in playlists:
        for song in pl.songs:
            entry = agg.setdefault(song_key(song), _SongAgg(song=song))
            entry.playlist_page_ids.add(pl_page_ids[pl.name])

    song_index = fetch_song_index(client, cfg["songs_data_source_id"])
    for key, entry in agg.items():
        existing = song_index.get(key)
        if existing is None:
            client.request(
                "POST",
                "/v1/pages",
                json={
                    "parent": {
                        "type": "data_source_id",
                        "data_source_id": cfg["songs_data_source_id"],
                    },
                    "properties": _song_props(entry.song, entry.playlist_page_ids),
                },
            )
            stats["songs_created"] += 1
        elif existing.relations_complete and not entry.playlist_page_ids <= existing.relation_ids:
            merged = existing.relation_ids | entry.playlist_page_ids
            client.request(
                "PATCH",
                f"/v1/pages/{existing.page_id}",
                json={
                    "properties": {"Playlists": {"relation": [{"id": i} for i in sorted(merged)]}}
                },
            )
            stats["songs_updated"] += 1
    return stats


# --- followed artists -----------------------------------------------------------


def artist_key(artist: FollowedArtist) -> str:
    return artist.spotify_id or artist.name.lower()


def fetch_artist_index(client: NotionClient, data_source_id: str) -> dict[str, str]:
    index = {}
    for pg in client.query_all(data_source_id):
        props = pg["properties"]
        index[_plain(props["Spotify ID"]) or _plain(props["Name"]).lower()] = pg["id"]
    return index


def _artist_props(artist: FollowedArtist, now_iso: str) -> dict:
    props = {
        "Name": {"title": [{"text": {"content": artist.name[:2000]}}]},
        "Spotify ID": _rt(artist.spotify_id),
        "Last Synced": {"date": {"start": now_iso}},
    }
    # export data has no genres/followers — omit rather than blank out live values
    if artist.genres:
        props["Genres"] = {"multi_select": [{"name": g} for g in artist.genres]}
    if artist.followers is not None:
        props["Followers"] = {"number": artist.followers}
    return props


def sync_followed_artists(client: NotionClient, cfg: dict, artists: list[FollowedArtist]) -> dict:
    """Upsert followed artists keyed on Spotify ID (fallback name). Rerun-safe."""
    now_iso = datetime.now(UTC).isoformat()
    stats = {"artists_created": 0, "artists_updated": 0}
    index = fetch_artist_index(client, cfg["followed_artists_data_source_id"])
    for artist in artists:
        props = _artist_props(artist, now_iso)
        page_id = index.get(artist_key(artist))
        if page_id:
            client.request("PATCH", f"/v1/pages/{page_id}", json={"properties": props})
            stats["artists_updated"] += 1
        else:
            client.request(
                "POST",
                "/v1/pages",
                json={
                    "parent": {
                        "type": "data_source_id",
                        "data_source_id": cfg["followed_artists_data_source_id"],
                    },
                    "properties": props,
                },
            )
            stats["artists_created"] += 1
        log.info("artist_synced", name=artist.name)
    return stats


def create_followed_artists_db(client: NotionClient, sandbox_page_id: str) -> dict:
    """Create the Followed Artists DB under the sandbox page; ids for sandbox_config.json."""
    db = client.request(
        "POST",
        "/v1/databases",
        json={
            "parent": {"type": "page_id", "page_id": sandbox_page_id},
            "title": [{"text": {"content": "Followed Artists"}}],
            "initial_data_source": {
                "properties": {
                    "Name": {"title": {}},
                    "Spotify ID": {"rich_text": {}},
                    "Genres": {"multi_select": {}},
                    "Followers": {"number": {}},
                    "Last Synced": {"date": {}},
                }
            },
        },
    )
    return {
        "followed_artists_database_id": db["id"],
        "followed_artists_data_source_id": db["data_sources"][0]["id"],
    }


# --- one-time sandbox creation --------------------------------------------------


def create_sandbox(client: NotionClient, project_page_id: str) -> dict:
    """Create the sandbox page + Playlists/Songs DBs; returns ids for sandbox_config.json."""
    sandbox = client.request(
        "POST",
        "/v1/pages",
        json={
            "parent": {"type": "page_id", "page_id": project_page_id},
            "properties": {"title": {"title": [{"text": {"content": "Spotify Sync Sandbox"}}]}},
        },
    )
    playlists_db = client.request(
        "POST",
        "/v1/databases",
        json={
            "parent": {"type": "page_id", "page_id": sandbox["id"]},
            "title": [{"text": {"content": "Playlists"}}],
            "initial_data_source": {
                "properties": {
                    "Name": {"title": {}},
                    "Description": {"rich_text": {}},
                    "Track Count": {"number": {}},
                    "Spotify ID": {"rich_text": {}},
                    "Last Synced": {"date": {}},
                }
            },
        },
    )
    pl_ds_id = playlists_db["data_sources"][0]["id"]
    songs_db = client.request(
        "POST",
        "/v1/databases",
        json={
            "parent": {"type": "page_id", "page_id": sandbox["id"]},
            "title": [{"text": {"content": "Songs"}}],
            "initial_data_source": {
                "properties": {
                    "Name": {"title": {}},
                    "Artist": {"rich_text": {}},
                    "Album": {"rich_text": {}},
                    "Spotify URI": {"rich_text": {}},
                    "Added": {"date": {}},
                    "Playlists": {
                        "relation": {
                            "data_source_id": pl_ds_id,
                            "type": "single_property",
                            "single_property": {},
                        }
                    },
                }
            },
        },
    )
    return {
        "sandbox_page_id": sandbox["id"],
        "playlists_database_id": playlists_db["id"],
        "playlists_data_source_id": pl_ds_id,
        "songs_database_id": songs_db["id"],
        "songs_data_source_id": songs_db["data_sources"][0]["id"],
    }
