"""Playlist covers: soma holds each playlist's cover, Music Sync applies it to Spotify.

A cover is a retained JPEG under raw/spotify-covers/<playlist id>/<sha256>.jpg named
by `playlists.cover_file`, with its hash in `cover_sha256` (written together, through
`just rules cover`). Reconcile uploads it only when that hash differs from what Music
Sync last applied (`cover_applied`) or Spotify now shows another image; a playlist
with no cover_file is left alone. Pure, apart from the hub calls in set_cover / seed.
"""

import base64
import hashlib
import re
from urllib.parse import urlsplit

PREFIX = "raw/spotify-covers/"
MAX_BASE64 = 256 * 1024  # Spotify's limit on the base64 JPEG payload


class CoverError(ValueError):
    pass


def key_for(playlist_id: str, sha256: str) -> str:
    return f"{PREFIX}{playlist_id}/{sha256}.jpg"


def _image_id(url: str | None) -> str:
    return urlsplit(url or "").path.rsplit("/", 1)[-1]


def image_key(url: str | None) -> str | None:
    """The image a Spotify image URL shows, whatever its CDN host or size variant: a
    40-hex image id is an 8-character kind, an 8-character size and the image itself."""
    image = _image_id(url)
    return (image[16:] if re.fullmatch(r"[0-9a-f]{40}", image) else image) or None


def is_custom(url: str | None) -> bool:
    """An uploaded playlist image, not a generated mosaic or album art."""
    # ponytail: Spotify's image-id kind prefix; recheck if Spotify changes its ids.
    return _image_id(url).startswith("ab67706c")


def validate(data: bytes) -> str | None:
    """Why Spotify would refuse this cover, or None."""
    if not data.startswith(b"\xff\xd8\xff"):
        return "is not a JPEG (Spotify takes a JPEG cover)"
    if len(base64.b64encode(data)) > MAX_BASE64:
        return "is over Spotify's 256 KB limit (about 192 KB of JPEG)"
    return None


def decide(playlist, current_url: str | None) -> str | None:
    """'upload', 'adopt' (record the image Spotify shows for an upload it had not shown
    yet) or None."""
    if not playlist.cover_file:
        return None
    applied = playlist.cover_applied or {}
    if applied.get("sha256") != playlist.cover_sha256:
        return "upload"
    if applied.get("spotify_url"):
        return None if image_key(applied["spotify_url"]) == image_key(current_url) else "upload"
    # Spotify still showed the old image right after the upload: adopt the new one once
    # it shows; still the old one a run later means the upload did not take.
    if current_url and image_key(current_url) != image_key(applied.get("replaced_url")):
        return "adopt"
    return "upload"


def applied(sha256: str, before: str | None, after: str | None) -> dict:
    """`cover_applied` after an upload, given what Spotify showed before and right after."""
    if after and image_key(after) != image_key(before):
        return {"sha256": sha256, "spotify_url": after}
    return {"sha256": sha256, "spotify_url": None, "replaced_url": before}


def set_cover(hub, mirror, playlist_id: str, data: bytes) -> dict:
    """Retain `data` as the playlist's cover and record it; the next reconcile uploads it."""
    if playlist_id not in mirror.playlists:
        raise CoverError("unknown playlist: classify it first (just rules curated <id>)")
    problem = validate(data)
    if problem:
        raise CoverError(f"the image {problem}")
    sha = hashlib.sha256(data).hexdigest()
    hub.put_file(key_for(playlist_id, sha), data, "image/jpeg")
    row = {"id": playlist_id, "cover_file": key_for(playlist_id, sha), "cover_sha256": sha}
    hub.push("playlists", [row])
    return row


def seed(hub, mirror, listing: list[dict], me: str, fetch, apply: bool = False) -> dict:
    """Seed cover_file from each owned, classified playlist's current custom Spotify image
    (`listing` = the owner's playlist objects, `fetch(url)` -> bytes). It is already on
    Spotify, so it is recorded as applied. Playlists that have a cover keep it."""
    plan = []
    for p in listing:
        known = mirror.playlists.get(p["id"])
        url = ((p.get("images") or [{}])[0]).get("url")
        if (p.get("owner") or {}).get("id") == me and known and not known.cover_file:
            if is_custom(url):
                plan.append({"id": p["id"], "name": known.name, "url": url})
    if not apply:
        return {"dry_run": True, "seed": plan}
    seeded = []
    for item in plan:
        data = fetch(item["url"])
        sha = hashlib.sha256(data).hexdigest()
        hub.put_file(key_for(item["id"], sha), data, "image/jpeg")
        hub.push(
            "playlists",
            [
                {
                    "id": item["id"],
                    "cover_file": key_for(item["id"], sha),
                    "cover_sha256": sha,
                    "cover_applied": {"sha256": sha, "spotify_url": item["url"]},
                }
            ],
        )
        seeded.append(item["id"])
    return {"dry_run": False, "seeded": seeded}
