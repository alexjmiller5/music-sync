"""Positive saved-track windows; partial observations are never removal evidence."""

import gzip
import hashlib
import re
import json
from datetime import datetime, timezone, timedelta

from core import archive, capture_clients
from core.model import ISRC_RE
import httpx
from uuid import uuid4
from core.spotify_client import SpotifyClient


def key(settings):
    return (
        "music-sync/quick-likes/"
        + hashlib.sha256(settings.workspace.encode()).hexdigest()
        + ".json.gz"
    )


def _load(settings, name):
    raw = archive.get(settings, name)
    return json.loads(gzip.decompress(raw)) if raw else None


def _save(settings, name, value):
    archive.put(settings, name, gzip.compress(json.dumps(value, sort_keys=True).encode()))


def _at(value):
    if not isinstance(value, str):
        raise ValueError("invalid saved timestamp")
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("saved timestamp requires timezone")
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def diff(previous, page):
    if (
        not isinstance(page, dict)
        or not isinstance(page.get("items"), list)
        or type(page.get("total")) is not int
        or page["total"] < len(page["items"])
        or type(page.get("offset")) is not int
        or page["offset"] != 0
        or "next" not in page
        or len(page["items"]) != min(page["total"], 50)
        or (page["total"] > 50 and not isinstance(page["next"], str))
    ):
        raise ValueError("invalid recent-like page")
    head, changes, last = {}, [], None
    for row in page["items"]:
        if not isinstance(row, dict) or not isinstance(row.get("track"), dict):
            raise ValueError("invalid saved track")
        tr = row["track"]
        tid = tr.get("id")
        if not isinstance(tid, str) or not tid or tid in head:
            raise ValueError("invalid or duplicate saved identity")
        if (
            tr.get("uri") != f"spotify:track:{tid}"
            or not re.fullmatch(ISRC_RE, (tr.get("external_ids") or {}).get("isrc") or "")
            or not tr.get("name")
            or not tr.get("artists")
        ):
            raise ValueError("incomplete saved recording identity")
        stamp = _at(row["added_at"])
        if last is not None and stamp > last:
            raise ValueError("recent-like page is not descending")
        last, head[tid] = stamp, stamp
        if previous is not None and previous.get(tid) != stamp:
            changes.append(row)
    if previous and head and not set(previous).intersection(head):
        raise ValueError("recent-like window has no overlap; wait for full sync")
    return head, changes


def _deliver(settings, spotify, hub, pending, now):
    from core import capture, mirror
    from core.hub import Hub, with_read_retries

    hub = hub or with_read_retries(Hub(settings.soma_hub_url, settings.soma_hub_token))
    completed = 0
    for event in pending["events"]:
        if event.get("done"):
            continue
        saved, tr = event["item"], event["item"]["track"]
        if event.get("outcome") in {"unknown", "added"}:
            playlists = mirror.load_playlists(hub)
            inbox = next(p.id for p in playlists.playlists.values() if p.kind == "inbox")
            items = [
                mirror.item_from_raw(r)
                for r in spotify.get_playlist_items(inbox, settings.spotify_market)
            ]
            isrc = (tr.get("external_ids") or {}).get("isrc")
            if not isrc or not any(row.isrc == isrc for row in items):
                pending["review_required"] = True
                _save(settings, archive.pending_key(settings), pending)
                raise ValueError(
                    "uncertain or added like lacks positive inbox evidence; review required"
                )

        def record(value):
            if event.get("outcome") == "added":
                return
            event["outcome"] = value
            _save(settings, archive.pending_key(settings), pending)

        identity = hashlib.sha256(
            json.dumps([tr["id"], _at(saved["added_at"])]).encode()
        ).hexdigest()
        payload = {
            "capture_id": identity,
            "title": tr.get("name") or "",
            "artist": ((tr.get("artists") or [{}])[0].get("name") or ""),
        }
        result = capture.capture(
            payload,
            spotify,
            hub,
            settings,
            now,
            resolved_track=tr,
            use_cache=False,
            record_outcome=record,
            liked_at=saved["added_at"],
            like_source_ref=pending["source_ref"],
            like_locator=event.get("locator"),
        )
        if not result.get("ok"):
            raise ValueError("quick-like delivery requires review")
        event["done"] = True
        _save(settings, archive.pending_key(settings), pending)
        completed += 1
    _save(settings, key(settings), pending["head"])
    _save(settings, archive.pending_key(settings), None)
    return {"completed": completed, "pending": False}


def run(
    settings,
    *,
    spotify=None,
    hub=None,
    now=None,
    dry_run=False,
    retry_review=False,
    scheduled_at=None,
):
    now = now or datetime.now(timezone.utc)
    if scheduled_at and (now - datetime.fromisoformat(_at(scheduled_at))).total_seconds() >= 900:
        return {"skipped": "stale_tick"}
    pending = _load(settings, archive.pending_key(settings))
    if retry_review and not pending:
        return {"skipped": "no_pending_recovery"}
    if pending and (pending.get("intent") != "quick_likes" or dry_run):
        return {"skipped": "pending_recovery"}
    if not dry_run and not settings.quick_likes_enabled:
        return {"skipped": "disabled"}
    if pending and pending.get("review_required") and not retry_review:
        return {"skipped": "review_required"}
    gate = capture_clients.spotify_retry_at(settings, now)
    if gate:
        return {"skipped": "cooldown", "retry_at": gate}
    spotify = spotify or SpotifyClient(settings, max_429_retries=0)
    try:
        if pending:
            return _deliver(settings, spotify, hub, pending, now)
        baseline = _load(settings, key(settings))
        page = spotify.get_recent_likes(settings.spotify_market)
        head, changes = diff(baseline, page)
        if dry_run:
            return {
                "valid": True,
                "items": len(head),
                "changes": len(changes),
                "baseline": baseline is not None,
            }
        if not changes:
            if baseline != head:
                _save(settings, key(settings), head)
            return {"baseline": baseline is None, "changes": 0}
        source = (
            "raw/spotify-pull/quick-likes/"
            + now.strftime("%Y%m%dT%H%M%S")
            + "-"
            + uuid4().hex
            + ".json.gz"
        )
        _save(
            settings, source, {"coverage": "partial", "observed_at": now.isoformat(), "page": page}
        )
        pending = {
            "intent": "quick_likes",
            "head": head,
            "source_ref": source,
            "events": [
                {
                    "item": item,
                    "outcome": None,
                    "done": False,
                    "locator": "/page/items/" + str(page["items"].index(item)),
                }
                for item in reversed(changes)
            ],
        }
        _save(settings, archive.pending_key(settings), pending)
        return _deliver(settings, spotify, hub, pending, now)
    except httpx.HTTPStatusError as exc:
        retry = capture_clients.defer_spotify(settings, exc, now)
        return {"error": "provider", "status": exc.response.status_code, "retry_at": retry}


def anchor(settings, saved):
    """A successful complete observation resets the window without replaying old likes."""
    head, _ = diff(
        None,
        {
            "items": [
                {"added_at": r["added_at"], "track": r.get("track") or r.get("item")}
                for r in saved[:50]
            ],
            "offset": 0,
            "total": len(saved),
            "next": "full-observation" if len(saved) > 50 else None,
        },
    )
    _save(settings, key(settings), head)


def cron_operation(now):
    utc = now.astimezone(timezone.utc)
    tick = utc - timedelta(minutes=(utc.minute - 10) % 15)
    return "reconcile" if tick.minute == 40 and tick.hour in {1, 7, 13, 19} else "quick_likes"
