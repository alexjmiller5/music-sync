"""Thin Spotify Web API client for the post-2026-02 Development Mode endpoint
set (plain Python, no Modal imports). No batch endpoints; search limit 10;
Save to Library takes uris as a QUERY parameter.

Token refresh, 401 refresh-retry, 429 Retry-After, and next-url pagination
are unit-tested with mocks.
"""

import time

import httpx
import structlog

from core.config import Settings

log = structlog.get_logger()

TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com"
ITEM_FIELDS = (
    "next,items(added_at,item(id,uri,name,is_local,is_playable,external_ids,"
    "artists(name),album(name,release_date)))"
)
MAX_429_RETRIES = 5


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
                time.sleep(wait)
                continue
            consecutive_429s = 0
            resp.raise_for_status()
            return resp.json() if resp.content else {}

    def _get(self, url: str, params: dict | None = None):
        return self._request("GET", url, params=params)

    def _paginate(self, url: str, params: dict) -> list[dict]:
        items, body = [], self._get(url, params)
        while True:
            items.extend(body.get("items") or [])
            if not body.get("next"):
                return items
            body = self._get(body["next"])

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
        return self._paginate(f"{API}/v1/me/tracks", {"limit": 50, "market": market})

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
    def add_items(self, playlist_id: str, uris: list[str]) -> None:
        for i in range(0, len(uris), 100):
            self._request(
                "POST", f"{API}/v1/playlists/{playlist_id}/items", json={"uris": uris[i : i + 100]}
            )

    def remove_items(self, playlist_id: str, uris: list[str]) -> None:
        for i in range(0, len(uris), 100):
            self._request(
                "DELETE",
                f"{API}/v1/playlists/{playlist_id}/items",
                json={"uris": uris[i : i + 100]},
            )

    def set_description(self, playlist_id: str, text: str) -> None:
        self._request("PUT", f"{API}/v1/playlists/{playlist_id}", json={"description": text[:300]})

    def like(self, uris: list[str]) -> None:
        for i in range(0, len(uris), 50):
            self._request(
                "PUT", f"{API}/v1/me/library", params={"uris": ",".join(uris[i : i + 50])}
            )
