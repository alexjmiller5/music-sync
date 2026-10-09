"""POST /capture: resolve a Shazam result on Spotify, add it to the inbox, record the edge."""

import gzip
import json
import re
from uuid import uuid4
from datetime import datetime

from core import archive, metadata, recognition
from core import mirror as mirror_mod
from core.config import Settings
from core.model import Live

_PUNCT = re.compile(r"[^\w\s]")


def _exact_norm(s: str) -> str:
    return " ".join(_PUNCT.sub("", s or "").casefold().split())


def best_match(title: str, artist: str, tracks: list[dict]) -> dict | None:
    title, artist = _exact_norm(title), _exact_norm(artist)
    for tr in tracks:
        names = [_exact_norm(x.get("name", "")) for x in tr.get("artists") or []]
        if _exact_norm(tr.get("name", "")) == title and artist in names:
            return tr
    return None


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def resolve_track(payload: dict, spotify, settings: Settings) -> dict | None:
    title, artist = (payload.get("title") or "").strip(), (payload.get("artist") or "").strip()
    if not title or not artist:
        return None
    if spotify is None:
        from core.spotify_client import SpotifyClient

        spotify = SpotifyClient(settings)
    if isrc := payload.get("isrc"):
        track = next(
            (
                track
                for track in spotify.search_isrc(isrc, settings.spotify_market)
                if (track.get("external_ids") or {}).get("isrc") == isrc
            ),
            None,
        )
    else:
        track = best_match(
            title, artist, spotify.search_track(title, artist, settings.spotify_market)
        )
    if track is None:
        return None
    resolved = mirror_mod.item_from_raw({"track": track})
    return track if resolved.isrc and resolved.track_id and resolved.uri else None


def capture(
    payload: dict,
    spotify,
    hub,
    settings: Settings,
    now: datetime,
    resolved_track: dict | None = None,
    record_outcome=None,
    client_id: str | None = None,
) -> dict:
    title, artist = (payload.get("title") or "").strip(), (payload.get("artist") or "").strip()
    if not title or not artist:
        return {"ok": False, "message": "title and artist required", "isrc": None}
    recognition.validate_time(payload.get("recognized_at"))
    pending = archive.get(settings, archive.pending_key(settings))
    if pending and json.loads(gzip.decompress(pending)):
        return {
            "ok": False,
            "message": "Pending recovery; retry after the original operation completes",
            "isrc": None,
        }
    from core.hub import Hub, with_read_retries
    from core.spotify_client import SpotifyClient

    spotify = spotify or SpotifyClient(settings)
    hub = hub or with_read_retries(Hub(settings.soma_hub_url, settings.soma_hub_token))
    song_columns = metadata.require_observed_contract(hub)
    tr = resolved_track if resolved_track is not None else resolve_track(payload, spotify, settings)
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
    cache = mirror_mod.load_cache(settings)
    m = mirror_mod.load_mirror(
        hub, cache=cache, checkpoint=lambda: mirror_mod.save_cache(settings, cache)
    )
    inbox = next((p for p in m.playlists.values() if p.kind == "inbox"), None)
    if not inbox:
        return {"ok": False, "message": "no inbox playlist in soma", "isrc": isrc}
    raw_items = spotify.get_playlist_items(inbox.id, settings.spotify_market)
    observed_items = [mirror_mod.item_from_raw(r) for r in raw_items]
    mirror_mod.validate_recording_aliases([resolved, *observed_items])
    matches = [it for it in observed_items if it.isrc == isrc]
    existing = matches[0] if matches else None
    if existing is not None and record_outcome:
        record_outcome("added")
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
                    "capture": {
                        key: payload[key]
                        for key in (
                            "capture_id",
                            "title",
                            "artist",
                            "apple_music_id",
                            "shazam_url",
                            "isrc",
                            "recognized_at",
                        )
                        if key in payload
                    },
                    "received_at": _iso(now),
                    "market": settings.spotify_market,
                }
            ).encode()
        ),
    )
    observations = metadata.observation_actions(
        m,
        Live({}, {}, {}, [resolved, *observed_items]),
        now,
        source_ref=source_ref,
        market=settings.spotify_market,
    )
    event_edge, event = recognition.retain_event(
        settings, payload, now, source_ref, isrc, client_id
    )
    now_s = _iso(now)
    if existing is None:
        if record_outcome:
            record_outcome("unknown")  # durable before the request can reach Spotify
        spotify.add_items(inbox.id, [tr["uri"]])
    if record_outcome:
        record_outcome("added")  # acknowledgement or observed inbox membership
    created = isrc not in m.songs
    song_rows = []
    for a in observations:
        if a.kind == "upsert_song":
            row = a.row
            if a.isrc not in m.songs:
                row = {"liked": 0, "liked_at": None, "first_seen": now_s, **row}
            song_rows.append(row)
    # The visible Shazam summary is recomputed from every retained event, so a
    # retried capture with the same identity never counts twice.
    # Written only once the catalog carries the summary columns.
    if set(recognition.PROJECTION) <= song_columns:
        shazam = recognition.song_projection(settings, hub, isrc, event_edge, event)
        for row in song_rows:
            if row["id"] == isrc:
                row.update(shazam)
                break
        else:
            base = {} if isrc in m.songs else {"liked": 0, "liked_at": None, "first_seen": now_s}
            song_rows.append({**base, **shazam})
    if song_rows:
        hub.push("songs", song_rows)
    hub.insert_rows("provenance", [event_edge])
    if len(matches) < 2:
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
    mirror_mod.validate_recording_aliases(items)
    extra = [i for i in items if i.isrc][
        : max(0, len([i for i in items if i.isrc]) - settings.inbox_cap)
    ]
    identities = [item.isrc for item in items if item.isrc]
    # Only recordings the catalog already holds in the inbox (or this capture) are
    # evicted; a hand-added song waits until reconciliation imports its history.
    cataloged = {i for (pid, i) in m.memberships if pid == inbox.id} | {isrc}
    extra = [i for i in extra if i.isrc in cataloged]
    if extra and len(set(identities)) == len(identities):
        spotify.remove_items(inbox.id, [i.uri for i in extra])
        hub.push(
            "playlist_songs", [{"id": f"{inbox.id}:{i.isrc}", "deleted_at": now_s} for i in extra]
        )
    mirror_mod.save_cache(settings, cache)
    message = "is already in" if existing else "added to"
    return {"ok": True, "message": f"{title} by {artist} {message} new songs", "isrc": isrc}
