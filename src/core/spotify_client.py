"""Thin Spotify Web API client (plain Python, no Modal imports).

LIVE CALLS ARE BLOCKED until Alex creates a Spotify developer app — 1Password
only holds his username/password today. Once SPOTIFY_CLIENT_ID/SECRET/
REFRESH_TOKEN exist (scripts/spotify_auth.py mints the refresh token), this
client works unchanged: token refresh, 401 refresh-retry, 429 Retry-After,
and cursor/next-url pagination are all covered by unit tests with mocks.
"""

import time

import httpx
import structlog

from core.config import Settings
from core.models import FollowedArtist

log = structlog.get_logger()

TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com"


class SpotifyClient:
    def __init__(self, settings: Settings, http: httpx.Client | None = None):
        if not (
            settings.spotify_client_id
            and settings.spotify_client_secret
            and settings.spotify_refresh_token
        ):
            raise RuntimeError(
                "Spotify credentials missing — create the developer app and run "
                "scripts/spotify_auth.py (see README)"
            )
        self._settings = settings
        self._http = http or httpx.Client(timeout=30)
        self._token: str | None = None

    def _refresh_token(self) -> None:
        resp = self._http.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self._settings.spotify_refresh_token,
            },
            auth=(self._settings.spotify_client_id, self._settings.spotify_client_secret),
        )
        resp.raise_for_status()
        self._token = resp.json()["access_token"]
        log.info("spotify_token_refreshed")

    def _get(self, url: str, params: dict | None = None) -> dict:
        if self._token is None:
            self._refresh_token()
        refreshed = False
        while True:
            resp = self._http.get(
                url, params=params, headers={"Authorization": f"Bearer {self._token}"}
            )
            if resp.status_code == 401 and not refreshed:
                self._refresh_token()
                refreshed = True
                continue
            if resp.status_code == 429:
                wait = float(resp.headers.get("Retry-After", "1"))
                log.warning("spotify_rate_limited", retry_after=wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()

    def _paginate(self, url: str, params: dict | None = None) -> list[dict]:
        """Offset-style pagination: follow the absolute 'next' url until null."""
        items = []
        body = self._get(url, params=params)
        while True:
            items.extend(body["items"])
            if not body.get("next"):
                return items
            body = self._get(body["next"])

    def get_followed_artists(self) -> list[FollowedArtist]:
        """All followed artists via cursor pagination on /v1/me/following."""
        artists, after = [], None
        while True:
            params = {"type": "artist", "limit": 50}
            if after:
                params["after"] = after
            body = self._get(f"{API}/v1/me/following", params=params)["artists"]
            artists.extend(
                FollowedArtist(
                    name=a["name"],
                    spotify_id=a["id"],
                    genres=a.get("genres", []),
                    followers=(a.get("followers") or {}).get("total"),
                )
                for a in body["items"]
            )
            after = (body.get("cursors") or {}).get("after")
            if not after:
                return artists

    def get_playlists(self) -> list[dict]:
        """Raw playlist objects for the current user (future live sync)."""
        return self._paginate(f"{API}/v1/me/playlists", params={"limit": 50})

    def get_playlist_tracks(self, playlist_id: str) -> list[dict]:
        """Raw track items for one playlist (future live sync)."""
        return self._paginate(f"{API}/v1/playlists/{playlist_id}/tracks", params={"limit": 100})


def merge_followed_artists(
    export: list[FollowedArtist], live: list[FollowedArtist]
) -> list[FollowedArtist]:
    """Live data wins per Spotify ID; export-only and live-only artists both kept."""
    merged = {a.spotify_id or a.name.lower(): a for a in export}
    merged.update({a.spotify_id or a.name.lower(): a for a in live})
    return list(merged.values())
