"""Archive the raw Spotify pull to R2 through Cloudflare's REST API (httpx only)."""

from datetime import datetime
from urllib.parse import quote

import httpx

from core.config import Settings


def key_for(now: datetime) -> str:
    return f"raw/spotify-pull/{now.strftime('%Y-%m-%dT%H%M%SZ')}.json.gz"


def put(settings: Settings, key: str, data: bytes, http: httpx.Client | None = None) -> None:
    url = (
        f"https://api.cloudflare.com/client/v4/accounts/{settings.r2_account_id}"
        f"/r2/buckets/{settings.r2_bucket}/objects/{quote(key, safe='')}"
    )
    r = (http or httpx.Client(timeout=120)).put(
        url,
        content=data,
        headers={
            "Authorization": f"Bearer {settings.r2_api_token}",
            "Content-Type": "application/gzip",
        },
    )
    r.raise_for_status()
