"""Modal deployment shim - ALL infrastructure lives here, as code.

Business logic stays in src/core/ (plain Python, no Modal imports). This file
maps it onto Modal: image, secrets, the hourly reconcile, operator endpoints,
and the app-issued-token capture endpoint.
"""

import os
from typing import Annotated

import httpx
import modal
import structlog
from fastapi import Header, Request

APP_NAME = "music-sync"  # also the Modal secret name (see justfile sync-secrets)

app = modal.App(APP_NAME)

image = (
    modal.Image.debian_slim(python_version="3.13")
    .uv_sync(extra_options="--no-dev")
    .add_local_dir("src/core", remote_path="/root/core", ignore=["**/__pycache__"])
)

secrets = [modal.Secret.from_name(APP_NAME)]


def _run(dry_run: bool, workspace: str = "default") -> dict:
    from core import run, workspaces
    from core.config import Settings

    settings = workspaces.settings_for(Settings(), workspace)
    if not dry_run and not workspaces.reconcile_enabled(settings):
        return {"skipped": True}
    log = run.reconcile(settings, dry_run=dry_run)
    return {
        "summary": log.summary(),
        "applied": dict(log.applied),
        "planned": log.planned,
        "flags": log.flags,
        "errors": log.errors,
    }


def _workspace_ids() -> list[str]:
    from core import workspaces
    from core.config import Settings

    return workspaces.ids(Settings())


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
    if operation == "workspace_admin":
        return _workspace_admin(body)
    if operation != "reconcile":
        raise ValueError("unknown operation")
    if "metadata_replay" in body:
        return _metadata_replay(body)
    # RECONCILE_ENABLED is the app-wide switch for cron and mutating runs;
    # non-default workspaces additionally need their own reconcile_enabled flag.
    dry_run = body.get("dry_run") is True
    if not dry_run and os.environ.get("RECONCILE_ENABLED") != "1":
        return {"skipped": True}
    if body.get("all_workspaces") is True:  # the hourly cron: every workspace in turn
        return {wid: _run(dry_run=False, workspace=wid) for wid in _workspace_ids()}
    if body.get("workspace"):
        return _run(dry_run=dry_run, workspace=body["workspace"])
    return _run(dry_run=dry_run)


@app.function(image=image, schedule=modal.Cron("0 * * * *"), timeout=1500)
def reconcile_cron():
    return worker.remote("reconcile", {"all_workspaces": True})


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


@app.function(image=image)
@modal.fastapi_endpoint(method="GET", label="capture-enroll")
def capture_enroll():
    """Static page an enrollment link opens; it forwards to offlineshazam://enroll."""
    from fastapi.responses import HTMLResponse

    from core.enroll_page import ENROLL_PAGE

    return HTMLResponse(ENROLL_PAGE, headers={"Cache-Control": "no-store"})


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
        if body.get("action") == "issue" and set(body) in (
            {"action", "label"},
            {"action", "label", "workspace"},
        ):
            from core import workspaces

            workspace = body.get("workspace") or "default"
            workspaces.settings_for(Settings(), workspace)  # must exist
            return capture_clients.issue(Settings(), body["label"], workspace=workspace)
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
    except KeyError as exc:  # workspaces.UnknownWorkspace
        return JSONResponse({"ok": False, "message": f"unknown workspace {exc}"}, status_code=404)
    except Exception:
        return JSONResponse({"ok": False, "message": "capture access unavailable"}, status_code=503)


def _consumer_capture(body: dict):
    from fastapi.responses import JSONResponse

    from core import capture_clients
    from core.config import Settings

    try:
        from core import capture as cap
        from core import workspaces

        base = Settings()
        capture_clients.require_active(base, body["client_id"])
        # The client's workspace decides whose Spotify, hub and Notion this touches.
        settings = workspaces.settings_for(
            base, capture_clients.client_workspace(base, body["client_id"])
        )
        result = capture_clients.deliver(
            settings,
            body["client_id"],
            body["capture"],
            lambda payload: cap.resolve_track(payload, None, settings),
            lambda payload, selected: _capture(payload, selected, settings),
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
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 429:
            # Spotify's retry budget is spent; tell clients how long to stay away
            # so their retries stop feeding the rate limit.
            return JSONResponse(
                {"ok": False, "message": "Spotify is rate limiting; retry later"},
                status_code=503,
                headers={"Retry-After": exc.response.headers.get("Retry-After", "300")},
            )
        return JSONResponse({"ok": False, "message": "capture unavailable"}, status_code=503)
    except Exception:
        # The client only sees a safe 503; the cause must be visible in the app logs.
        structlog.get_logger().exception("consumer_capture_failed")
        return JSONResponse({"ok": False, "message": "capture unavailable"}, status_code=503)


def _workspace_admin(body: dict):
    """Operator actions behind scripts/workspace.py (operator Modal auth only)."""
    from datetime import datetime, timezone

    from core import spotify_connect, workspaces
    from core.config import Settings

    base = Settings()
    action = body.get("action")
    if action == "list":
        return {"workspaces": workspaces.ids(base)}
    if action == "show":
        return workspaces.summary(base, body["workspace"])
    if action == "save":
        workspaces.save(base, body["workspace"], **body.get("fields", {}))
        return workspaces.summary(base, body["workspace"])
    if action == "connect_link":
        return {
            "invite": spotify_connect.issue_invite(
                base, body["workspace"], datetime.now(timezone.utc)
            )
        }
    raise ValueError(f"unknown workspace action {action!r}")


@app.function(image=image, secrets=secrets, timeout=60)
@modal.fastapi_endpoint(method="GET", label="spotify-connect")
def spotify_connect_endpoint(
    request: Request,
    invite: str | None = None,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    """Connect Spotify (core/spotify_connect.py): `?invite=` sends the person to
    Spotify's consent screen; Spotify returns here with `code` + `state`. This
    URL, with a trailing slash, is the redirect URI registered on the app."""
    from datetime import datetime, timezone

    from fastapi.responses import HTMLResponse, RedirectResponse

    from core import spotify_connect
    from core.config import Settings

    def page(title: str, text: str, status: int = 200):
        return HTMLResponse(spotify_connect.page(title, text), status_code=status)

    settings = Settings()
    now = datetime.now(timezone.utc)
    redirect_uri = f"https://{request.url.hostname}/"
    try:
        if error:
            return page(
                "Not connected", "Spotify access was not granted. You can close this page.", 400
            )
        if code and state:
            spotify_connect.complete(settings, state, code, redirect_uri, now, httpx.Client())
            return page(
                "Spotify connected",
                "Music Sync can now manage your playlists. You can close this page.",
            )
        if invite:
            return RedirectResponse(
                spotify_connect.authorize_url(settings, invite, redirect_uri, now)
            )
        return page("Connect Spotify", "This link is incomplete. Ask for a new one.", 400)
    except spotify_connect.InvalidInvite as exc:
        return page("Link not valid", str(exc), 400)
    except httpx.HTTPError:
        return page("Spotify did not answer", "Try the link again in a minute.", 502)


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


def _capture(body: dict, resolved_track: dict | None = None, settings=None):
    from datetime import datetime, timezone

    from fastapi.responses import JSONResponse

    from core import capture as cap
    from core.config import Settings
    from core.spotify_client import SpotifyAuthError

    s = settings or Settings()
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
        _flag_quietly(
            s,
            [],
            [
                f"Spotify refresh token {e}: reconnect with `just workspace connect-link {s.workspace}`"
            ],
        )
        return JSONResponse(
            {"ok": False, "message": "Spotify token expired; flagged"}, status_code=503
        )
    if not out["ok"] and out["message"].startswith("Could not find"):
        _flag_quietly(
            s, [f"Add {body.get('title')} by {body.get('artist')} to new songs manually"], []
        )
    return out
