"""Retain individual recognition events independently of current playlist membership."""

import gzip
import json
from datetime import datetime, timezone
from hashlib import sha256
from uuid import uuid4

from core import archive


def validate_time(value: str | None) -> None:
    if value is None:
        return
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError("timezone required")
    except (TypeError, ValueError) as exc:
        raise ValueError("recognized_at requires a timezone-aware ISO timestamp") from exc


def retain(settings, payload, now, source_ref, isrc, client_id=None):
    """Serialized caller preserves the first retained event across delivery retries.

    Legacy callers without capture IDs get independent events; their retries cannot
    be distinguished from new recognitions. No server atomicity is assumed.
    """
    validate_time(payload.get("recognized_at"))
    identity = [
        settings.workspace,
        client_id or "operator",
        payload.get("capture_id") or str(uuid4()),
    ]
    digest = sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
    key = f"raw/spotify-capture/events/{digest}.json.gz"
    existing = archive.get(settings, key)
    if existing is not None:
        event = json.loads(gzip.decompress(existing))["event"]
        if event["payload"] != payload or event["isrc"] != isrc:
            raise ValueError("capture ID already retained with different payload or recording")
    else:
        event = {
            "payload": payload,
            "isrc": isrc,
            "recognized_at": payload.get("recognized_at"),
            "received_at": now.astimezone(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "observation_ref": source_ref,
        }
        archive.put(settings, key, gzip.compress(json.dumps({"event": event}).encode()))
    return {
        "id": f"shazam-event:{digest}:{isrc}",
        "from_kind": "shazam",
        "from_ref": f"{key}#/event",
        "to_kind": "songs",
        "to_ref": isrc,
        "rel": "imported_from",
        "asserted_by": "music-sync",
        "detail": {},
    }
