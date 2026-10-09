"""Retain individual recognition events independently of current playlist membership.

Every event has its own retained original (`/event`) and one create-only provenance
row. The songs projection (shazamed, count, first/last date, estimated flag) is a
maintained summary recomputed from those originals alone, so retries never double
count. Exact recognition times come only from the recognizer; playlist add dates
and server receipt times are labeled estimates.
"""

import gzip
import json
from datetime import datetime, timezone
from hashlib import sha256
from uuid import uuid4

from core import archive

PROJECTION = (
    "shazamed",
    "shazam_count",
    "shazam_first_at",
    "shazam_last_at",
    "shazam_dates_estimated",
)
HISTORY_PREFIX = "raw/spotify-capture/events/historical/"
PROV_COLS = ["id", "from_ref", "to_ref", "detail", "deleted_at"]


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
    """The event's create-only provenance row; see `retain_event`."""
    return retain_event(settings, payload, now, source_ref, isrc, client_id)[0]


def retain_event(settings, payload, now, source_ref, isrc, client_id=None):
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
    return _edge(key, isrc, event["received_at"], digest), event


def _edge(key, isrc, stamp, digest):
    return {
        "id": f"shazam-event:{digest}:{isrc}",
        "from_kind": "takeout",
        "from_ref": key,
        "to_kind": "songs",
        "to_ref": isrc,
        "rel": "evidence_of",
        "asserted_by": "music-sync",
        "field": None,
        "detail": {"kind": "shazam_recognition", "locator": "/event"},
        "updated_at": stamp,
    }


def _utc(value):
    return (
        datetime.fromisoformat(value)
        .astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def event_date(event):
    """(UTC timestamp or None, estimated). Receipt time is never a recognition time."""
    if event.get("recognized_at"):
        return _utc(event["recognized_at"]), False
    for key in ("estimated_recognized_at", "received_at"):
        if event.get(key):
            return _utc(event[key]), True
    return None, True


def project(isrc, events):
    dates = [event_date(event) for event in events]
    known = [d for d, _ in dates if d]
    return {
        "id": isrc,
        "shazamed": 1,
        "shazam_count": len(dates),
        "shazam_first_at": min(known) if known else None,
        "shazam_last_at": max(known) if known else None,
        "shazam_dates_estimated": int(any(estimated for _, estimated in dates)),
    }


def _detail(row):
    value = row.get("detail")
    return json.loads(value) if isinstance(value, str) else value or {}


def _recognitions(rows):
    return {
        row["id"]: row
        for row in rows
        if not row.get("deleted_at") and _detail(row).get("kind") == "shazam_recognition"
    }


def projections(settings, rows, known=None):
    """Songs projection rows from recognition provenance rows plus their originals."""
    known = dict(known or {})
    by_song = {}
    for row in _recognitions(rows).values():
        key = row["from_ref"]
        if key not in known:
            raw = archive.get(settings, key)
            if raw is None:
                raise ValueError(f"recognition original missing: {key}")
            known[key] = json.loads(gzip.decompress(raw))["event"]
        by_song.setdefault(row["to_ref"], []).append(known[key])
    return [project(isrc, events) for isrc, events in sorted(by_song.items())]


def song_projection(settings, hub, isrc, edge, event):
    """One song's projection including an event whose row may not be stored yet."""
    rows = hub.pull(
        "provenance",
        PROV_COLS,
        where={"to_ref": isrc, "rel": "evidence_of", "asserted_by": "music-sync"},
    )
    return projections(settings, [*rows, edge], {edge["from_ref"]: event})[0]


def import_history(settings, hub, events, now):
    """Create-only import of past recognitions with explicitly estimated dates.

    `events`: evidence_id, isrc, estimated_recognized_at, estimate_basis, source_ref,
    source_locator. Retained originals are written once; a changed original stops.
    """
    known, edges = {}, []
    stamp = now.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    for item in events:
        digest = sha256(json.dumps([item["evidence_id"], item["isrc"]]).encode()).hexdigest()
        key = f"{HISTORY_PREFIX}{digest}.json.gz"
        event = {
            "kind": "historical_recognition",
            "isrc": item["isrc"],
            "recognized_at": None,
            "estimated_recognized_at": item["estimated_recognized_at"],
            "estimate_basis": item["estimate_basis"],
            "source_ref": item["source_ref"],
            "source_locator": item.get("source_locator"),
            "evidence_id": item["evidence_id"],
        }
        existing = archive.get(settings, key)
        if existing is None:
            archive.put(settings, key, gzip.compress(json.dumps({"event": event}).encode()))
        elif json.loads(gzip.decompress(existing))["event"] != event:
            raise ValueError(f"retained recognition differs: {item['evidence_id']}")
        known[key] = event
        edges.append(_edge(key, item["isrc"], stamp, digest))
    inserted = existing_rows = 0
    for i in range(0, len(edges), 100):
        receipt = hub.insert_rows("provenance", edges[i : i + 100])
        inserted += len(receipt["inserted"])
        existing_rows += len(receipt["existing"])
    return {"events": len(edges), "inserted": inserted, "existing": existing_rows, "known": known}


def legacy_capture_events(rows):
    """Capture edges written before event originals existed: receipt time is an estimate."""
    out = []
    for row in rows:
        if row.get("deleted_at") or row.get("from_ref") == row.get("to_ref"):
            continue  # whole-history playlist imports are covered by playlist occurrences
        out.append(
            {
                "evidence_id": f"legacy-capture:{row['id']}",
                "isrc": row["to_ref"],
                "estimated_recognized_at": row["created_at"],
                "estimate_basis": "capture receipt time (provenance created_at)",
                "source_ref": f"provenance:{row['id']}",
                "source_locator": None,
            }
        )
    return out
