"""POST /capture: resolve a Shazam result on Spotify, add it to the inbox, record the edge."""

import re
from datetime import datetime

from core import mirror as mirror_mod
from core.config import Settings
from core.model import ISRC_RE

_STRIP = re.compile(r"\s*[\(\[\-].*$")
_PUNCT = re.compile(r"[^\w\s]")


def _norm(s: str) -> str:
    return _PUNCT.sub("", _STRIP.sub("", s or "")).casefold().strip()


def best_match(title: str, artist: str, tracks: list[dict]) -> dict | None:
    t, a = _norm(title), _norm(artist)
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
    tr = best_match(title, artist, spotify.search_track(title, artist, settings.spotify_market))
    if not tr:
        return {
            "ok": False,
            "message": f"Could not find {title} by {artist} on Spotify",
            "isrc": None,
        }
    isrc = ((tr.get("external_ids") or {}).get("isrc") or "").upper()
    if not re.match(ISRC_RE, isrc):
        return {"ok": False, "message": f"{title} by {artist} has no ISRC on Spotify", "isrc": None}
    m = mirror_mod.load_mirror(hub)
    inbox = next((p for p in m.playlists.values() if p.kind == "inbox"), None)
    if not inbox:
        return {"ok": False, "message": "no inbox playlist in life-data", "isrc": isrc}
    if (inbox.id, isrc) in m.memberships:
        return {"ok": True, "message": f"{title} by {artist} is already in new songs", "isrc": isrc}
    now_s = _iso(now)
    spotify.add_items(inbox.id, [tr["uri"]])
    created = isrc not in m.songs
    if created:
        hub.push("songs", [{"id": isrc, "liked": 0, "liked_at": None, "first_seen": now_s}])
    hub.push(
        "playlist_songs",
        [
            {
                "id": f"{inbox.id}:{isrc}",
                "playlist_id": inbox.id,
                "isrc": isrc,
                "spotify_track_id": tr["id"],
                "added_at": now_s,
                "deleted_at": None,
            }
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
            }
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
    return {"ok": True, "message": f"{title} by {artist} added to new songs", "isrc": isrc}
