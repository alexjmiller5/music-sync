"""Modal deployment shim - ALL infrastructure lives here, as code.

Business logic stays in src/core/ (plain Python, no Modal imports). This file
maps it onto Modal: image, secrets, the hourly reconcile, operator endpoints,
and the app-issued-token capture endpoint.
"""

import os
from typing import Annotated

import modal
from fastapi import Header

APP_NAME = "music-sync"  # also the Modal secret name (see justfile sync-secrets)

app = modal.App(APP_NAME)

image = (
    modal.Image.debian_slim(python_version="3.13")
    .uv_sync(extra_options="--no-dev")
    .add_local_dir("src/core", remote_path="/root/core", ignore=["**/__pycache__"])
)

secrets = [modal.Secret.from_name(APP_NAME)]


def _run(dry_run: bool) -> dict:
    from core import run
    from core.config import Settings

    log = run.reconcile(Settings(), dry_run=dry_run)
    return {
        "summary": log.summary(),
        "applied": dict(log.applied),
        "planned": log.planned,
        "flags": log.flags,
        "errors": log.errors,
    }


@app.function(image=image, secrets=secrets, max_containers=1, timeout=1500)
@modal.concurrent(max_inputs=1)
def worker(operation: str, body: dict | None = None):
    """One queue for the entire read/archive/plan/apply cycle across all callers."""
    body = body or {}
    if operation == "capture":
        return _capture(body)
    if operation == "consumer_capture":
        return _consumer_capture(body)
    if operation == "capture_access":
        return _capture_access(body)
    if operation != "reconcile":
        raise ValueError("unknown operation")
    if "metadata_replay" in body:
        return _metadata_replay(body)
    dry_run = body.get("dry_run") is True
    if not dry_run and os.environ.get("RECONCILE_ENABLED") != "1":
        return {"skipped": True}
    return _run(dry_run=dry_run)


@app.function(image=image, schedule=modal.Cron("0 * * * *"), timeout=1500)
def reconcile_cron():
    return worker.remote("reconcile")


@app.function(image=image, timeout=1500)
@modal.fastapi_endpoint(method="POST", requires_proxy_auth=True)
def reconcile(body: dict | None = None):
    return worker.remote("reconcile", body)


@app.function(image=image, timeout=1500)
@modal.fastapi_endpoint(method="POST", requires_proxy_auth=True)
def capture(body: dict):
    return worker.remote("capture", body)


@app.function(image=image, secrets=secrets, timeout=1500)
@modal.fastapi_endpoint(method="POST", label="capture-consumer")
def capture_consumer(body: dict, authorization: Annotated[str | None, Header()] = None):
    from fastapi.responses import JSONResponse

    from core import capture_clients
    from core.config import Settings

    scheme, separator, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not separator or not token:
        return JSONResponse(
            {"ok": False, "message": "unauthorized"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        client_id = capture_clients.authenticate(Settings(), token)
    except capture_clients.Unauthorized:
        return JSONResponse(
            {"ok": False, "message": "unauthorized"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
    except Exception:
        return JSONResponse({"ok": False, "message": "capture unavailable"}, status_code=503)
    return worker.remote("consumer_capture", {"client_id": client_id, "capture": body})


@app.function(image=image, timeout=1500)
@modal.fastapi_endpoint(method="POST", label="capture-access", requires_proxy_auth=True)
def capture_access(body: dict):
    return worker.remote("capture_access", body)


def _capture_access(body: dict):
    from fastapi.responses import JSONResponse

    from core import capture_clients
    from core.config import Settings

    try:
        if not isinstance(body, dict):
            raise capture_clients.InvalidRequest("body must be an object")
        if body.get("action") == "issue" and set(body) == {"action", "label"}:
            return capture_clients.issue(Settings(), body["label"])
        if body.get("action") == "revoke" and set(body) == {"action", "client_id"}:
            revoked = capture_clients.revoke(Settings(), body["client_id"])
            if not revoked:
                return JSONResponse(
                    {"ok": False, "message": "capture client not found"}, status_code=404
                )
            return {"ok": True, "revoked": True}
        raise capture_clients.InvalidRequest("action must be issue or revoke with exact fields")
    except capture_clients.InvalidRequest as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=422)
    except Exception:
        return JSONResponse({"ok": False, "message": "capture access unavailable"}, status_code=503)


def _consumer_capture(body: dict):
    from fastapi.responses import JSONResponse

    from core import capture_clients
    from core.config import Settings

    try:
        settings = Settings()
        from core import capture as cap

        capture_clients.require_active(settings, body["client_id"])
        result = capture_clients.deliver(
            settings,
            body["client_id"],
            body["capture"],
            lambda payload: cap.resolve_track(payload, None, settings),
            lambda payload, selected: _capture(payload, selected),
        )
        if result.get("ok") is not True:
            return JSONResponse(result, status_code=422)
        return result
    except capture_clients.Unauthorized:
        return JSONResponse(
            {"ok": False, "message": "unauthorized"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
    except capture_clients.InvalidRequest as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=422)
    except capture_clients.Conflict as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=409)
    except Exception:
        return JSONResponse({"ok": False, "message": "capture unavailable"}, status_code=503)


def _metadata_replay(body: dict):
    from fastapi.responses import JSONResponse

    from core import metadata_replay
    from core.config import Settings
    from core.hub import HubError

    try:
        request = body["metadata_replay"]
        if (
            not isinstance(request, dict)
            or set(request) != {"archive_key", "observed_at"}
            or set(body) - {"metadata_replay", "dry_run"}
        ):
            raise ValueError("metadata_replay requires exactly archive_key and observed_at")
        dry_run = body.get("dry_run", True)
        metadata_replay.validate_request(**request, dry_run=dry_run)
        return metadata_replay.run(Settings(), **request, dry_run=dry_run)
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        status = (
            422
            if isinstance(exc, ValueError)
            else 404
            if isinstance(exc, FileNotFoundError)
            else 503
            if isinstance(exc, HubError)
            else 409
        )
        return JSONResponse({"errors": [{"code": status, "message": str(exc)}]}, status_code=status)


def _flag_quietly(settings, flags: list[str], errors: list[str]) -> None:
    """flags.file (Notion) can fail on its own; never let that turn the
    documented capture response into an opaque 500."""
    from datetime import date

    import httpx
    from core import flags as flags_mod

    try:
        flags_mod.file(settings, httpx.Client(), flags, errors, date.today().isoformat())
    except Exception as e:
        print(f"capture: could not file flag: {e}")


def _capture(body: dict, resolved_track: dict | None = None):
    from datetime import datetime, timezone

    from fastapi.responses import JSONResponse

    from core import capture as cap
    from core.config import Settings
    from core.spotify_client import SpotifyAuthError

    s = Settings()
    try:
        out = cap.capture(
            body or {},
            None,
            None,
            s,
            datetime.now(timezone.utc),
            resolved_track=resolved_track,
        )
    except SpotifyAuthError as e:
        _flag_quietly(s, [], [f"Spotify refresh token {e}: re-mint with scripts/spotify_auth.py"])
        return JSONResponse(
            {"ok": False, "message": "Spotify token expired; flagged"}, status_code=503
        )
    if not out["ok"] and out["message"].startswith("Could not find"):
        _flag_quietly(
            s, [f"Add {body.get('title')} by {body.get('artist')} to new songs manually"], []
        )
    return out
