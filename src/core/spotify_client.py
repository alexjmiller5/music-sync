"""Thin Spotify Web API client for the post-2026-02 Development Mode endpoint
set (plain Python, no Modal imports). No batch endpoints; search limit 10;
Save to Library takes uris as a QUERY parameter.

Token refresh, 401 refresh-retry, 429 Retry-After, and next-url pagination
are unit-tested with mocks.
"""

import time
from urllib.parse import urlsplit

import httpx
import structlog

from core.config import Settings

log = structlog.get_logger()

TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com"
ITEM_FIELDS = (
    "next,total,offset,items(added_at,item(id,uri,name,is_local,is_playable,external_ids,"
    "duration_ms,linked_from(id),artists(name),album(name,release_date)))"
)
MAX_429_RETRIES = 5
# A throttled Spotify can ask for minutes or hours; the single worker must not
# sleep that long with every other caller queued behind it. Longer waits fail
# fast and reach clients as 503 + Retry-After so they back off instead.
MAX_429_WAIT = 30.0


class SpotifyAuthError(RuntimeError):
    """The refresh token is dead (invalid_grant). Re-mint with scripts/spotify_auth.py."""


class SpotifyClient:
    def __init__(self, settings: Settings, http: httpx.Client | None = None):
        self._s = settings
        self._http = http or httpx.Client(timeout=30)
        self._token: str | None = None

    def _refresh_token(self) -> None:
        resp = self._http.post(
            TOKEN_URL,
            data={"grant_type": "refresh_token", "refresh_token": self._s.spotify_refresh_token},
            auth=(self._s.spotify_client_id, self._s.spotify_client_secret),
        )
        if resp.status_code == 400 and "invalid_grant" in resp.text:
            raise SpotifyAuthError(f"invalid_grant: {resp.json().get('error_description', '')}")
        resp.raise_for_status()
        self._token = resp.json()["access_token"]

    def _request(self, method: str, url: str, **kwargs):
        if self._token is None:
            self._refresh_token()
        refreshed = False
        consecutive_429s = 0
        while True:
            resp = self._http.request(
                method, url, headers={"Authorization": f"Bearer {self._token}"}, **kwargs
            )
            if resp.status_code == 401 and not refreshed:
                self._refresh_token()
                refreshed = True
                consecutive_429s = 0
                continue
            if resp.status_code == 429:
                consecutive_429s += 1
                if consecutive_429s > MAX_429_RETRIES:
                    resp.raise_for_status()
                wait = float(resp.headers.get("Retry-After", "1"))
                log.warning("spotify_rate_limited", retry_after=wait)
                if wait > MAX_429_WAIT:
                    resp.raise_for_status()
                time.sleep(wait)
                continue
            consecutive_429s = 0
            resp.raise_for_status()
            return resp.json() if resp.content else {}

    def _get(self, url: str, params: dict | None = None):
        return self._request("GET", url, params=params)

    def _paginate(self, url: str, params: dict, *, page_key: str | None = None) -> list[dict]:
        items, body = [], self._get(url, params)
        origin = urlsplit(url)
        seen = {url}
        expected_total = None
        while True:
            if page_key and isinstance(body, dict):
                body = body.get(page_key)
            if (
                not isinstance(body, dict)
                or not isinstance(body.get("items"), list)
                or any(not isinstance(item, dict) for item in body["items"])
                or "next" not in body
                or type(body.get("total")) is not int
                or body["total"] < 0
            ):
                raise ValueError("Spotify pagination: incomplete page")
            if expected_total is None:
                expected_total = body["total"]
            if body["total"] != expected_total:
                raise ValueError("Spotify pagination: total changed during collection")
            if "offset" in body and (
                type(body["offset"]) is not int or body["offset"] != len(items)
            ):
                raise ValueError("Spotify pagination: unexpected offset")
            items.extend(body["items"])
            next_url = body["next"]
            if next_url is None:
                if len(items) != expected_total:
                    raise ValueError("Spotify pagination: incomplete total")
                return items
            if not isinstance(next_url, str) or not next_url or next_url in seen:
                raise ValueError("Spotify pagination: invalid or repeated next URL")
            target = urlsplit(next_url)
            if (
                (target.scheme, target.netloc, target.path)
                != (origin.scheme, origin.netloc, origin.path)
                or target.fragment
                or not body["items"]
                or len(items) >= expected_total
            ):
                raise ValueError("Spotify pagination: invalid continuation")
            seen.add(next_url)
            body = self._get(next_url)

    # reads
    def me(self) -> dict:
        return self._get(f"{API}/v1/me")

    def get_playlists(self) -> list[dict]:
        return self._paginate(f"{API}/v1/me/playlists", {"limit": 50})

    def get_playlist(self, playlist_id: str) -> dict:
        return self._get(f"{API}/v1/playlists/{playlist_id}")

    def get_playlist_items(self, playlist_id: str, market: str) -> list[dict]:
        return self._paginate(
            f"{API}/v1/playlists/{playlist_id}/items",
            {"limit": 50, "market": market, "fields": ITEM_FIELDS},
        )

    def get_liked(self, market: str) -> list[dict]:
        items = self._paginate(f"{API}/v1/me/tracks", {"limit": 50, "market": market})
        for item in items:
            track = item.get("track")
            if (
                not isinstance(track, dict)
                or not isinstance(track.get("id"), str)
                or not track["id"]
            ):
                raise ValueError("Spotify pagination: unidentified liked item")
        return items

    def get_followed_artists(self) -> list[dict]:
        rows = self._paginate(
            f"{API}/v1/me/following", {"type": "artist", "limit": 50}, page_key="artists"
        )
        ids = [row.get("id") for row in rows]
        if any(not isinstance(id, str) or not id for id in ids) or len(set(ids)) != len(ids):
            raise ValueError("Spotify pagination: invalid or repeated followed artist identity")
        return rows

    def get_track(self, track_id: str, market: str) -> dict:
        return self._get(f"{API}/v1/tracks/{track_id}", {"market": market})

    def _search(self, q: str, market: str) -> list[dict]:
        body = self._get(
            f"{API}/v1/search", {"q": q, "type": "track", "limit": 10, "market": market}
        )
        return ((body.get("tracks") or {}).get("items")) or []

    def search_isrc(self, isrc: str, market: str) -> list[dict]:
        return self._search(f"isrc:{isrc}", market)

    def search_track(self, title: str, artist: str, market: str) -> list[dict]:
        return self._search(f"track:{title} artist:{artist}", market)

    # writes
    def add_items(
        self, playlist_id: str, uris: list[str], position: int | None = None
    ) -> str | None:
        snapshot = None
        for i in range(0, len(uris), 100):
            body = {"uris": uris[i : i + 100]}
            if position is not None:
                body["position"] = position + i
            snapshot = self._request(
                "POST", f"{API}/v1/playlists/{playlist_id}/items", json=body
            ).get("snapshot_id")
        return snapshot

    def remove_items(self, playlist_id: str, uris: list[str]) -> str | None:
        """Removes EVERY occurrence of each URI; Spotify no longer accepts positions."""
        snapshot = None
        for i in range(0, len(uris), 100):
            snapshot = self._request(
                "DELETE",
                f"{API}/v1/playlists/{playlist_id}/items",
                json={"items": [{"uri": uri} for uri in uris[i : i + 100]]},
            ).get("snapshot_id")
        return snapshot

    def create_playlist(self, name: str, description: str, public: bool = False) -> dict:
        return self._request(
            "POST",
            f"{API}/v1/me/playlists",
            json={"name": name, "public": public, "description": description[:300]},
        )

    def rename_playlist(self, playlist_id: str, name: str) -> None:
        self._request("PUT", f"{API}/v1/playlists/{playlist_id}", json={"name": name})

    def set_description(self, playlist_id: str, text: str) -> None:
        self._request("PUT", f"{API}/v1/playlists/{playlist_id}", json={"description": text[:300]})

    def unfollow_playlist(self, playlist_id: str) -> None:
        self._request(
            "DELETE", f"{API}/v1/me/library", params={"uris": f"spotify:playlist:{playlist_id}"}
        )

    def like(self, uris: list[str]) -> None:
        for i in range(0, len(uris), 40):
            self._request(
                "PUT", f"{API}/v1/me/library", params={"uris": ",".join(uris[i : i + 40])}
            )

    def unlike(self, uris: list[str]) -> None:
        for i in range(0, len(uris), 40):
            self._request(
                "DELETE", f"{API}/v1/me/library", params={"uris": ",".join(uris[i : i + 40])}
            )
