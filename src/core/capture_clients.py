"""Capture-only client credentials and idempotent delivery receipts."""

import gzip
import hmac
import json
import secrets
from hashlib import sha256
from uuid import UUID, uuid4

from core import archive
from core.config import Settings

CLIENTS_KEY = "music-sync/capture-clients.json.gz"
RECEIPTS_PREFIX = "music-sync/capture-receipts"
_PAYLOAD_FIELDS = {"capture_id", "title", "artist", "apple_music_id", "shazam_url"}


class Unauthorized(Exception):
    pass


class InvalidRequest(ValueError):
    pass


class Conflict(Exception):
    pass


def _load(settings: Settings, key: str):
    data = archive.get(settings, key)
    return None if data is None else json.loads(gzip.decompress(data))


def _save(settings: Settings, key: str, value) -> None:
    archive.put(settings, key, gzip.compress(json.dumps(value, sort_keys=True).encode()))


def issue(settings: Settings, label: str) -> dict:
    if not isinstance(label, str) or not label.strip():
        raise InvalidRequest("label must be a nonempty string")
    token = secrets.token_urlsafe(32)
    client_id = str(uuid4())
    registry = _load(settings, CLIENTS_KEY) or {"clients": []}
    registry["clients"].append(
        {
            "client_id": client_id,
            "label": label.strip(),
            "token_hash": sha256(token.encode()).hexdigest(),
            "revoked": False,
        }
    )
    _save(settings, CLIENTS_KEY, registry)
    return {"ok": True, "client_id": client_id, "token": token}


def revoke(settings: Settings, client_id: str) -> bool:
    try:
        client_id = str(UUID(client_id))
    except (AttributeError, TypeError, ValueError) as exc:
        raise InvalidRequest("client_id must be a UUID") from exc
    registry = _load(settings, CLIENTS_KEY) or {"clients": []}
    for client in registry["clients"]:
        if client["client_id"] == client_id:
            client["revoked"] = True
            _save(settings, CLIENTS_KEY, registry)
            return True
    return False


def authenticate(settings: Settings, token: str | None) -> str:
    if not token:
        raise Unauthorized
    digest = sha256(token.encode()).hexdigest()
    registry = _load(settings, CLIENTS_KEY) or {"clients": []}
    for client in registry["clients"]:
        if hmac.compare_digest(client["token_hash"], digest) and not client["revoked"]:
            return client["client_id"]
    raise Unauthorized


def validate_payload(payload: dict) -> dict:
    if not isinstance(payload, dict) or set(payload) != _PAYLOAD_FIELDS:
        raise InvalidRequest(
            "capture requires exactly capture_id, title, artist, apple_music_id and shazam_url"
        )
    if not all(isinstance(payload[field], str) for field in _PAYLOAD_FIELDS):
        raise InvalidRequest("capture fields must be strings")
    if not payload["title"].strip() or not payload["artist"].strip():
        raise InvalidRequest("title and artist must be nonempty")
    try:
        capture_id = str(UUID(payload["capture_id"]))
    except ValueError as exc:
        raise InvalidRequest("capture_id must be a UUID") from exc
    return {
        **payload,
        "capture_id": capture_id,
        "title": payload["title"].strip(),
        "artist": payload["artist"].strip(),
    }


def _payload_hash(payload: dict) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode()).hexdigest()


def deliver(settings: Settings, client_id: str, payload: dict, perform) -> dict:
    payload = validate_payload(payload)
    capture_id = payload["capture_id"]
    key = f"{RECEIPTS_PREFIX}/{client_id}/{capture_id}.json.gz"
    digest = _payload_hash(payload)
    receipt = _load(settings, key)
    if receipt is not None:
        if not hmac.compare_digest(receipt["payload_hash"], digest):
            raise Conflict("capture_id was already used with a different payload")
        return {"ok": True, "capture_id": capture_id, "isrc": receipt["isrc"]}

    result = perform(payload)
    if result.get("ok") is not True:
        return result
    isrc = result.get("isrc")
    if not isinstance(isrc, str) or not isrc:
        raise RuntimeError("capture succeeded without an ISRC")
    _save(
        settings,
        key,
        {"capture_id": capture_id, "payload_hash": digest, "isrc": isrc},
    )
    return {"ok": True, "capture_id": capture_id, "isrc": isrc}
