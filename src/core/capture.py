"""POST /capture: resolve a Shazam result on Spotify, add it to the inbox, record the edge."""

import gzip
import json
import re
from uuid import uuid4
from datetime import datetime

from core import archive, metadata
from core import mirror as mirror_mod
from core.config import Settings
from core.model import Live

_STRIP = re.compile(r"\s+-.*$|\s*[\(\[].*$")
_PUNCT = re.compile(r"[^\w\s]")


def _norm(s: str) -> str:
    return _PUNCT.sub("", _STRIP.sub("", s or "")).casefold().strip()


def _exact_norm(s: str) -> str:
    return _PUNCT.sub("", s or "").casefold().strip()


def best_match(title: str, artist: str, tracks: list[dict]) -> dict | None:
    t, a = _norm(title), _norm(artist)
    exact_title = _exact_norm(title)
    for tr in tracks:
        names = [_norm(x.get("name", "")) for x in tr.get("artists") or []]
        if _exact_norm(tr.get("name", "")) == exact_title and any(a in n or n in a for n in names):
            return tr
    for tr in tracks:
        names = [_norm(x.get("name", "")) for x in tr.get("artists") or []]
        if _norm(tr.get("name", "")) == t and any(a in n or n in a for n in names):
            return tr
    for tr in tracks:
        if _norm(tr.get("name", "")) == t:
            return tr
    return None


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def capture(payload: dict, spotify, hub, settings: Settings, now: datetime) -> dict:
    title, artist = (payload.get("title") or "").strip(), (payload.get("artist") or "").strip()
    if not title or not artist:
        return {"ok": False, "message": "title and artist required", "isrc": None}
    pending = archive.get(settings, archive.PENDING_KEY)
    if pending and json.loads(gzip.decompress(pending)):
        return {
            "ok": False,
            "message": "Pending recovery; retry after the original operation completes",
            "isrc": None,
        }
    from core.hub import Hub
    from core.spotify_client import SpotifyClient

    spotify = spotify or SpotifyClient(settings)
    hub = hub or Hub(settings.life_hub_url, settings.life_hub_token)
    metadata.require_observed_contract(hub)
    tr = best_match(title, artist, spotify.search_track(title, artist, settings.spotify_market))
    if not tr:
        return {
            "ok": False,
            "message": f"Could not find {title} by {artist} on Spotify",
            "isrc": None,
        }
    resolved = mirror_mod.item_from_raw({"track": tr})
    isrc = resolved.isrc
    if not isrc:
        return {"ok": False, "message": f"{title} by {artist} has no ISRC on Spotify", "isrc": None}
    m = mirror_mod.load_mirror(hub)
    inbox = next((p for p in m.playlists.values() if p.kind == "inbox"), None)
    if not inbox:
        return {"ok": False, "message": "no inbox playlist in life-data", "isrc": isrc}
    raw_items = spotify.get_playlist_items(inbox.id, settings.spotify_market)
    source_ref = f"raw/spotify-capture/{now.strftime('%Y-%m-%dT%H%M%S')}-{uuid4().hex}.json.gz"
    archive.put(
        settings,
        source_ref,
        gzip.compress(
            json.dumps(
                {
                    "playlist_id": inbox.id,
                    "items": raw_items,
                    "resolved_track": tr,
                }
            ).encode()
        ),
    )
    observed_items = [mirror_mod.item_from_raw(r) for r in raw_items]
    existing = next((it for it in observed_items if it.isrc == isrc), None)
    observations = metadata.observation_actions(
        m,
        Live({}, {}, {}, [resolved, *observed_items]),
        now,
        source_ref=source_ref,
        market=settings.spotify_market,
    )
    now_s = _iso(now)
    if existing is None:
        spotify.add_items(inbox.id, [tr["uri"]])
    created = isrc not in m.songs
    song_rows = []
    for a in observations:
        if a.kind == "upsert_song":
            row = a.row
            if a.isrc not in m.songs:
                row = {"liked": 0, "liked_at": None, "first_seen": now_s, **row}
            song_rows.append(row)
    if song_rows:
        hub.push("songs", song_rows)
    hub.push(
        "playlist_songs",
        [
            {
                "id": f"{inbox.id}:{isrc}",
                "playlist_id": inbox.id,
                "isrc": isrc,
                "spotify_track_id": existing.track_id if existing else tr["id"],
                "added_at": existing.added_at if existing else now_s,
                "deleted_at": None,
            },
        ],
    )
    ref = str(payload.get("apple_music_id") or isrc)
    hub.push(
        "provenance",
        [
            {
                "id": f"shazam:{ref}:{isrc}",
                "from_kind": "shazam",
                "from_ref": ref,
                "to_kind": "songs",
                "to_ref": isrc,
                "rel": "imported_from",
                "asserted_by": "music-sync",
                "detail": {
                    "created_row": 1 if created else 0,
                    "shazam_url": payload.get("shazam_url"),
                },
            },
            *(a.row for a in observations if a.kind == "edge"),
        ],
    )
    items = sorted(
        (
            mirror_mod.item_from_raw(i)
            for i in spotify.get_playlist_items(inbox.id, settings.spotify_market)
        ),
        key=lambda x: x.added_at,
    )
    extra = [i for i in items if i.isrc][
        : max(0, len([i for i in items if i.isrc]) - settings.inbox_cap)
    ]
    if extra:
        spotify.remove_items(inbox.id, [i.uri for i in extra])
        hub.push(
            "playlist_songs", [{"id": f"{inbox.id}:{i.isrc}", "deleted_at": now_s} for i in extra]
        )
    message = "is already in" if existing else "added to"
    return {"ok": True, "message": f"{title} by {artist} {message} new songs", "isrc": isrc}
