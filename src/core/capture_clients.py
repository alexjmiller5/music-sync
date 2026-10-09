"""Capture-only client credentials and idempotent delivery receipts."""

import gzip
import hmac
import json
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from urllib.parse import urlencode
from uuid import UUID, uuid4

import httpx

from core import archive, recognition
from core.config import Settings

CLIENTS_KEY = "music-sync/capture-clients.json.gz"
RECEIPTS_PREFIX = "music-sync/capture-receipts"
# One empty marker per capture still owed Spotify or catalog work, so a drain
# lists only open work. The gate holds Spotify's Retry-After for every client.
QUEUE_PREFIX = "music-sync/capture-queue"
SPOTIFY_GATE_KEY = "music-sync/capture-spotify-gate.json.gz"
NO_MATCH_RECHECK = timedelta(days=1)
_REQUIRED_PAYLOAD_FIELDS = {"capture_id", "title", "artist", "apple_music_id", "shazam_url"}
_PAYLOAD_FIELDS = _REQUIRED_PAYLOAD_FIELDS | {"isrc", "recognized_at"}
_ISRC = re.compile(r"[A-Z]{2}[A-Z0-9]{3}\d{7}")


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


def issue(settings: Settings, label: str, workspace: str = "default") -> dict:
    if not isinstance(label, str) or not label.strip():
        raise InvalidRequest("label must be a nonempty string")
    token = secrets.token_urlsafe(32)
    client_id = str(uuid4())
    registry = _load(settings, CLIENTS_KEY) or {"clients": []}
    registry["clients"].append(
        {
            "client_id": client_id,
            "label": label.strip(),
            "workspace": workspace,
            "token_hash": sha256(token.encode()).hexdigest(),
            "revoked": False,
        }
    )
    _save(settings, CLIENTS_KEY, registry)
    return {"ok": True, "client_id": client_id, "token": token}


def enrollment_link(enroll_page_url: str, capture_url: str, token: str) -> str:
    """The token rides in the URL fragment, which browsers never send to the
    page's server; the page hands it to offlineshazam://enroll."""
    return f"{enroll_page_url}#{urlencode({'url': capture_url, 'token': token})}"


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


def client_workspace(settings: Settings, client_id: str) -> str:
    """The workspace a client's captures belong to (clients issued before
    workspaces existed belong to the default one)."""
    registry = _load(settings, CLIENTS_KEY) or {"clients": []}
    for client in registry["clients"]:
        if client["client_id"] == client_id:
            return client.get("workspace", "default")
    raise Unauthorized


def require_active(settings: Settings, client_id: str) -> None:
    registry = _load(settings, CLIENTS_KEY) or {"clients": []}
    if not any(
        client["client_id"] == client_id and not client["revoked"] for client in registry["clients"]
    ):
        raise Unauthorized


def validate_payload(payload: dict) -> dict:
    if (
        not isinstance(payload, dict)
        or not _REQUIRED_PAYLOAD_FIELDS <= set(payload)
        or not set(payload) <= _PAYLOAD_FIELDS
    ):
        raise InvalidRequest(
            "capture requires capture_id, title, artist, apple_music_id and shazam_url; "
            "isrc and recognized_at are optional"
        )
    if not all(isinstance(value, str) for value in payload.values()):
        raise InvalidRequest("capture fields must be strings")
    if not payload["title"].strip() or not payload["artist"].strip():
        raise InvalidRequest("title and artist must be nonempty")
    try:
        recognition.validate_time(payload.get("recognized_at"))
    except ValueError as exc:
        raise InvalidRequest(str(exc)) from exc
    try:
        capture_id = str(UUID(payload["capture_id"]))
    except ValueError as exc:
        raise InvalidRequest("capture_id must be a UUID") from exc
    validated = {
        **payload,
        "capture_id": capture_id,
        "title": payload["title"].strip(),
        "artist": payload["artist"].strip(),
    }
    if "isrc" in payload:
        isrc = payload["isrc"].replace("-", "").upper()
        if not _ISRC.fullmatch(isrc):
            raise InvalidRequest("isrc must be a valid ISRC")
        validated["isrc"] = isrc
    return validated


def _payload_hash(payload: dict) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode()).hexdigest()


class DeliveryFailure(Exception):
    """A validated capture failed; its Spotify effect is independent of HTTP success."""

    def __init__(self, capture_id: str, outcome: str):
        super().__init__("capture unavailable")
        self.capture_id = capture_id
        self.outcome = outcome


def deliver(settings: Settings, client_id: str, payload: dict, select, perform) -> dict:
    payload = validate_payload(payload)
    capture_id = payload["capture_id"]
    key = f"{RECEIPTS_PREFIX}/{client_id}/{capture_id}.json.gz"
    digest = _payload_hash(payload)
    state = _load(settings, key)
    if state is not None:
        if not hmac.compare_digest(state["payload_hash"], digest):
            raise Conflict("capture_id was already used with a different payload")
        if "isrc" in state:
            return {
                "ok": True,
                "capture_id": capture_id,
                "isrc": state["isrc"],
                "spotify_outcome": "added",
            }
        # A queued capture has not been attempted; legacy incomplete receipts
        # cannot prove whether Spotify was.
        outcome = state.get("spotify_outcome", "not_added" if "payload" in state else "unknown")
    else:
        state = {"capture_id": capture_id, "payload_hash": digest}
        outcome = "not_added"

    def record_outcome(value):
        nonlocal outcome
        # Added evidence survives all later maintenance failures and retries.
        if outcome == "added":
            return
        outcome = value
        state["spotify_outcome"] = value
        _save(settings, key, state)

    try:
        if "selected_track" not in state:
            selected = select(payload)
            if selected is None:
                return {
                    "ok": False,
                    "capture_id": capture_id,
                    "spotify_outcome": outcome,
                    "reason": "no_match",
                    "message": f"Could not find {payload['title']} by {payload['artist']} on Spotify",
                    "isrc": None,
                }
            state["selected_track"] = selected
            state["spotify_outcome"] = outcome
            _save(settings, key, state)

        result = perform(payload, state["selected_track"], record_outcome)
        if result.get("ok") is not True:
            return {**result, "capture_id": capture_id, "spotify_outcome": outcome}
        isrc = result.get("isrc")
        if not isinstance(isrc, str) or not isrc:
            raise RuntimeError("capture succeeded without an ISRC")
        outcome = "added"
        state.pop("selected_track", None)
        _save(settings, key, {**state, "isrc": isrc, "spotify_outcome": "added"})
        return {
            "ok": True,
            "capture_id": capture_id,
            "isrc": isrc,
            "spotify_outcome": outcome,
        }
    except Exception as exc:
        raise DeliveryFailure(capture_id, outcome) from exc


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _receipt_key(client_id: str, capture_id: str) -> str:
    return f"{RECEIPTS_PREFIX}/{client_id}/{capture_id}.json.gz"


def _queue_key(client_id: str, capture_id: str) -> str:
    return f"{QUEUE_PREFIX}/{client_id}/{capture_id}.json.gz"


def view(state: dict) -> dict:
    """What a client may know about one capture."""
    payload = state.get("payload") or {}
    outcome = "added" if "isrc" in state else state.get("spotify_outcome", "not_added")
    status = "added" if outcome == "added" else state.get("status", "queued")
    return {
        "capture_id": state["capture_id"],
        "status": status,
        "spotify_outcome": outcome,
        "isrc": state.get("isrc"),
        "title": payload.get("title"),
        "artist": payload.get("artist"),
        "retry_at": None if status == "added" else state.get("retry_at"),
        "reason": state.get("reason"),
    }


def accept(settings: Settings, client_id: str, payload: dict, now: datetime) -> dict:
    """Durably take a capture for later delivery; a repeat reports its current state."""
    payload = validate_payload(payload)
    capture_id = payload["capture_id"]
    key = _receipt_key(client_id, capture_id)
    digest = _payload_hash(payload)
    state = _load(settings, key)
    if state is not None:
        if not hmac.compare_digest(state["payload_hash"], digest):
            raise Conflict("capture_id was already used with a different payload")
        if "payload" in state or "isrc" in state:
            return view(state)
    # New, or a receipt from synchronous delivery: keep its selection and outcome
    # (an incomplete one of those cannot prove whether Spotify was attempted).
    if state is not None:
        state.setdefault("spotify_outcome", "unknown")
    state = {
        **(state or {"capture_id": capture_id, "payload_hash": digest}),
        "payload": payload,
        "status": "queued",
        "received_at": _iso(now),
    }
    _save(settings, key, state)
    archive.put(settings, _queue_key(client_id, capture_id), gzip.compress(b"{}"))
    return view(state)


def status(settings: Settings, client_id: str, capture_id: str) -> dict | None:
    state = _load(settings, _receipt_key(client_id, capture_id))
    return None if state is None else view(state)


def _retry_after(exc: BaseException | None) -> tuple[float | None, bool]:
    """A rate-limited service's Retry-After, and whether that service is Spotify."""
    if not isinstance(exc, httpx.HTTPStatusError) or exc.response.status_code not in (429, 503):
        return None, False
    spotify = str(exc.request.url.host).endswith("spotify.com") and exc.response.status_code == 429
    try:
        return max(0.0, float(exc.response.headers.get("Retry-After", "300"))), spotify
    except ValueError:
        return 300.0, spotify


def _backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(3600, 60 * 2**attempts))


def drain(settings: Settings, step, now: datetime, deadline: float | None = None) -> dict:
    """Deliver queued captures oldest first through `step(client_id, payload, received_at)`.

    `step` runs the synchronous delivery and raises Unauthorized or DeliveryFailure.
    A Spotify rate limit stops the drain and gates every capture until its
    Retry-After; any other failure backs the capture off and stops the drain.
    """
    summary = {"processed": 0, "added": 0, "deferred_until": None}
    gate = _load(settings, SPOTIFY_GATE_KEY)
    if gate and _parse(gate["retry_at"]) > now:
        summary["deferred_until"] = gate["retry_at"]
        return summary
    due = []
    for marker in archive.keys(settings, QUEUE_PREFIX + "/"):
        client_id, name = marker[len(QUEUE_PREFIX) + 1 :].split("/", 1)
        capture_id = name.removesuffix(".json.gz")
        state = _load(settings, _receipt_key(client_id, capture_id))
        if state is None or "payload" not in state or "isrc" in state:
            archive.delete(settings, marker)
            continue
        if state.get("retry_at") and _parse(state["retry_at"]) > now:
            continue
        due.append((state["received_at"], client_id, capture_id))
    for _, client_id, capture_id in sorted(due):
        if deadline is not None and time.monotonic() > deadline:
            break
        summary["processed"] += 1
        if _drain_one(settings, step, client_id, capture_id, now, summary):
            break
    return summary


def _drain_one(settings, step, client_id, capture_id, now, summary) -> bool:
    """Deliver one capture and record what happened; True stops the drain."""
    key = _receipt_key(client_id, capture_id)
    state = _load(settings, key)
    attempts = state.get("attempts", 0)

    def record(**fields):
        latest = _load(settings, key) or state  # delivery may have advanced the receipt
        outcome = "added" if "isrc" in latest else latest.get("spotify_outcome")
        status = fields.pop("status", "added" if outcome == "added" else "queued")
        _save(settings, key, {**latest, "status": status, **fields})

    try:
        result = step(client_id, state["payload"], state["received_at"])
    except Unauthorized:
        record(status="rejected", retry_at=None)
        archive.delete(settings, _queue_key(client_id, capture_id))
        return False
    except Exception as exc:
        wait, spotify = _retry_after(exc.__cause__ if isinstance(exc, DeliveryFailure) else exc)
        if spotify:
            retry_at = _iso(now + timedelta(seconds=wait))
            _save(settings, SPOTIFY_GATE_KEY, {"retry_at": retry_at})
            record(retry_at=retry_at, attempts=attempts + 1, reason=None)
            return True
        delay = max(_backoff(attempts), timedelta(seconds=wait or 0))
        record(retry_at=_iso(now + delay), attempts=attempts + 1, reason=None)
        return True
    if result.get("ok") is True:
        record(status="added", retry_at=None, reason=None)
        archive.delete(settings, _queue_key(client_id, capture_id))
        summary["added"] += 1
        return False
    if result.get("reason") == "no_match":
        record(status="not_added", reason="no_match", retry_at=_iso(now + NO_MATCH_RECHECK))
        return False
    record(retry_at=_iso(now + _backoff(attempts)), attempts=attempts + 1, reason=None)
    return str(result.get("message", "")).startswith("Pending recovery")
