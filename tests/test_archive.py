import gzip
from datetime import datetime, timezone

import httpx

from core import archive


def test_put_uses_cf_r2_objects_api(settings):
    seen = {}

    def handler(req):
        seen["m"], seen["url"], seen["h"], seen["body"] = (
            req.method,
            str(req.url),
            req.headers,
            req.content,
        )
        return httpx.Response(200, json={"success": True})

    archive.put(
        settings,
        "raw/spotify-pull/x.json.gz",
        gzip.compress(b"{}"),
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert seen["m"] == "PUT"
    assert (
        seen["url"]
        == "https://api.cloudflare.com/client/v4/accounts/acct/r2/buckets/bucket/objects/raw%2Fspotify-pull%2Fx.json.gz"
    )
    assert (
        seen["h"]["Authorization"] == "Bearer r2tok"
        and seen["h"]["Content-Type"] == "application/gzip"
    )
    assert gzip.decompress(seen["body"]) == b"{}"


def test_key_for():
    now = datetime(2026, 9, 8, 13, 5, 9, tzinfo=timezone.utc)
    key = archive.key_for(now)
    assert key.startswith("raw/spotify-pull/2026-09-08T130509Z-")
    assert key.endswith(".json.gz")
    assert key != archive.key_for(now)
