"""Connect Spotify: a person approves Music Sync with their own Spotify login.

The operator issues a single-use, expiring invite for a workspace
(`just workspace connect-link <id>`); the link opens the spotify-connect
endpoint, which sends the person to Spotify's consent screen for this app's
client id. Spotify redirects back with a code, exchanged here (with the app's
client secret, server-side) for that person's refresh token, stored on their
workspace. No developer credential or script is involved in a user's
approval. Spotify's Development Mode also requires the person's Spotify email
on the app's user allowlist in the Spotify dashboard.
"""

import gzip
import json
import secrets
from datetime import datetime, timedelta
from hashlib import sha256
from urllib.parse import urlencode

import httpx

from core import archive, workspaces
from core.config import Settings

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
# Same scopes as scripts/spotify_auth.py (a test keeps them equal).
SCOPES = (
    "playlist-read-private playlist-read-collaborative "
    "playlist-modify-private playlist-modify-public "
    "user-library-read user-library-modify user-follow-read"
)
INVITES_PREFIX = "music-sync/spotify-invites"


class InvalidInvite(ValueError):
    pass


def _key(invite: str) -> str:
    return f"{INVITES_PREFIX}/{sha256(invite.encode()).hexdigest()}.json.gz"


def _load(settings: Settings, invite: str) -> dict | None:
    data = archive.get(settings, _key(invite))
    return json.loads(gzip.decompress(data)) if data else None


def _save(settings: Settings, invite: str, record: dict) -> None:
    archive.put(settings, _key(invite), gzip.compress(json.dumps(record).encode()))


def issue_invite(settings: Settings, workspace: str, now: datetime, ttl=timedelta(days=7)) -> str:
    workspaces.settings_for(settings, workspace)  # must exist
    invite = secrets.token_urlsafe(24)
    _save(
        settings,
        invite,
        {"workspace": workspace, "expires_at": (now + ttl).isoformat(), "used": False},
    )
    return invite


def _active(settings: Settings, invite: str, now: datetime) -> dict:
    record = _load(settings, invite) if invite else None
    if not record or record["used"] or datetime.fromisoformat(record["expires_at"]) <= now:
        raise InvalidInvite("this link is invalid, expired or already used - ask for a new one")
    return record


def authorize_url(settings: Settings, invite: str, redirect_uri: str, now: datetime) -> str:
    _active(settings, invite, now)
    query = {
        "client_id": settings.spotify_client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "state": invite,
    }
    return f"{AUTHORIZE_URL}?{urlencode(query)}"


def complete(
    settings: Settings, state: str, code: str, redirect_uri: str, now: datetime, http: httpx.Client
) -> str:
    """Exchange Spotify's code for the person's refresh token; returns the workspace."""
    record = _active(settings, state, now)
    response = http.post(
        TOKEN_URL,
        data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri},
        auth=(settings.spotify_client_id, settings.spotify_client_secret),
        timeout=30,
    )
    response.raise_for_status()
    refresh = response.json().get("refresh_token")
    if not refresh:
        raise InvalidInvite("Spotify did not return a refresh token")
    workspaces.save(settings, record["workspace"], spotify_refresh_token=refresh)
    _save(settings, state, {**record, "used": True})
    return record["workspace"]


def page(title: str, text: str) -> str:
    """The small page the connect endpoint answers with (static, no user data)."""
    from html import escape

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="referrer" content="no-referrer">
<title>{escape(title)}</title><style>
:root{{color-scheme:light dark;--bg:#fafafa;--fg:#111;--muted:#666}}
@media (prefers-color-scheme:dark){{:root{{--bg:#111;--fg:#eee;--muted:#999}}}}
body{{margin:0;min-height:100dvh;display:grid;place-items:center;background:var(--bg);color:var(--fg);
font:16px/1.5 -apple-system,BlinkMacSystemFont,system-ui,sans-serif}}
main{{max-width:26rem;padding:2rem 1.5rem;text-align:center}}h1{{font-size:1.5rem;margin:0 0 .5rem}}
p{{color:var(--muted);margin:0}}</style></head>
<body><main><h1>{escape(title)}</h1><p>{escape(text)}</p></main></body></html>"""
