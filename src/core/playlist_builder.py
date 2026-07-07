"""Build Spotify playlists from track lists (plain Python, no Modal imports).

Additive-only: creates a new private playlist and adds tracks — never touches
existing playlists. Needs live Spotify creds (see scripts/spotify_auth.py).
"""

from itertools import batched

import structlog

from core.spotify_client import API, SpotifyClient

log = structlog.get_logger()


def find_track_uri(client: SpotifyClient, name: str, artist: str) -> str | None:
    """First search hit for track+artist, or None."""
    body = client._get(
        f"{API}/v1/search",
        params={"q": f'track:"{name}" artist:"{artist}"', "type": "track", "limit": 1},
    )
    items = body["tracks"]["items"]
    return items[0]["uri"] if items else None


def create_playlist(
    client: SpotifyClient, name: str, description: str | None, track_uris: list[str]
) -> dict:
    """Create a private playlist for the current user and add tracks in batches of 100."""
    user_id = client._get(f"{API}/v1/me")["id"]
    playlist = client._post(
        f"{API}/v1/users/{user_id}/playlists",
        json={"name": name, "description": description or "", "public": False},
    )
    for batch in batched(track_uris, 100):
        client._post(f"{API}/v1/playlists/{playlist['id']}/tracks", json={"uris": list(batch)})
    log.info("playlist_created", name=name, tracks=len(track_uris))
    return playlist
