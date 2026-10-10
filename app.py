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
    try:
        log = run.reconcile(settings, dry_run=dry_run, deadline=_budget())
    except Exception as exc:
        # A stage failure before a run log exists (catalog contract, mirror load,
        # Spotify pull, archive) still reaches the flag destination. Only the
        # category is reported: exception text can carry response data.
        errors = [f"reconcile stopped before applying anything: {type(exc).__name__}"]
        structlog.get_logger().error("reconcile_failed", error_type=type(exc).__name__)
        if not dry_run:
            _flag_quietly(settings, [], errors)
        return {"errors": errors}
    return {
        "summary": log.summary(),
        "applied": dict(log.applied),
        "planned": log.planned,
        "review_items": log.review_items,
        "flags": log.flags,
        "errors": log.errors,
    }


WORKER_TIMEOUT = 3600
APPLY_BUDGET = 3000  # seconds of a call after which apply stops cleanly and stays pending
DRAIN_TIMEOUT = 900
DRAIN_BUDGET = 600  # seconds of a drain call after which remaining captures wait for the next one


def _budget() -> float:
    import time

    return time.monotonic() + APPLY_BUDGET


def _workspace_ids() -> list[str]:
    from core import workspaces
    from core.config import Settings

    return workspaces.ids(Settings())


@app.function(image=image, secrets=secrets, max_containers=1, timeout=WORKER_TIMEOUT)
@modal.concurrent(max_inputs=1)
def worker(operation: str, body: dict | None = None):
    """One queue for the entire read/archive/plan/apply cycle across all callers."""
    body = body or {}
    if operation == "capture":
        return _capture(body)
    if operation == "capture_access":
        return _capture_access(body)
    if operation == "workspace_admin":
        return _workspace_admin(body)
    if operation in ROLLOUT:
        return ROLLOUT[operation](body)
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


@app.function(image=image, schedule=modal.Cron("0 * * * *"), timeout=WORKER_TIMEOUT)
def reconcile_cron():
    # Hourly backstop for captures waiting on a Retry-After or their daily recheck.
    _spawn_drain()
    return worker.remote("reconcile", {"all_workspaces": True})


@app.function(image=image, timeout=WORKER_TIMEOUT)
@modal.fastapi_endpoint(method="POST", requires_proxy_auth=True)
def reconcile(body: dict | None = None):
    return worker.remote("reconcile", body)


@app.function(image=image, timeout=WORKER_TIMEOUT)
@modal.fastapi_endpoint(method="POST", requires_proxy_auth=True)
def capture(body: dict):
    return worker.remote("capture", body)


@app.function(image=image, secrets=secrets, timeout=60)
@modal.asgi_app(label="capture-consumer")
def capture_consumer():
    """Accept-and-queue intake: POST / stores a capture and answers 202 at once;
    GET /<capture_id> reports what the drain has done with it."""
    return _capture_api()


@app.function(image=image, secrets=secrets, max_containers=1, timeout=DRAIN_TIMEOUT)
@modal.concurrent(max_inputs=1)
def capture_drain():
    """Delivers accepted captures to Spotify, outside the serialized worker."""
    import time
    from datetime import datetime, timezone

    from core import capture_clients
    from core.config import Settings

    return capture_clients.drain(
        Settings(),
        _deliver_queued,
        datetime.now(timezone.utc),
        deadline=time.monotonic() + DRAIN_BUDGET,
    )


def _spawn_drain() -> None:
    try:
        capture_drain.spawn()
    except Exception as exc:  # the next request, status read or cron retries it
        structlog.get_logger().error("capture_drain_spawn_failed", error_type=type(exc).__name__)


def _capture_api():
    from datetime import datetime, timezone
    from math import ceil
    from uuid import UUID

    from fastapi import BackgroundTasks, FastAPI
    from fastapi.responses import JSONResponse

    from core import capture_clients
    from core.config import Settings

    api = FastAPI()

    def client_for(authorization: str | None):
        scheme, separator, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not separator or not token:
            raise capture_clients.Unauthorized
        return capture_clients.authenticate(Settings(), token)

    def unauthorized():
        return JSONResponse(
            {"ok": False, "message": "unauthorized"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )

    def retry_after(view: dict, now: datetime) -> dict:
        if not view["retry_at"]:
            return {}
        due = datetime.fromisoformat(view["retry_at"].replace("Z", "+00:00"))
        return {"Retry-After": str(max(0, ceil((due - now).total_seconds())))}

    def due(view: dict, now: datetime) -> bool:
        return view["status"] in ("queued", "not_added") and not retry_after(view, now).get(
            "Retry-After", "0"
        ).strip("0")

    @api.post("/")
    def accept(
        body: dict,
        background: BackgroundTasks,
        authorization: Annotated[str | None, Header()] = None,
    ):
        try:
            client_id = client_for(authorization)
        except capture_clients.Unauthorized:
            return unauthorized()
        except Exception:
            return JSONResponse({"ok": False, "message": "capture unavailable"}, status_code=503)
        now = datetime.now(timezone.utc)
        try:
            view = capture_clients.accept(Settings(), client_id, body, now)
        except capture_clients.InvalidRequest as exc:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=422)
        except capture_clients.Conflict as exc:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=409)
        except Exception as exc:
            # Never render exception locals: settings and request credentials may be present.
            structlog.get_logger().error("capture_accept_failed", error_type=type(exc).__name__)
            return JSONResponse(
                {
                    "ok": False,
                    "message": "capture unavailable",
                    "capture_id": body.get("capture_id"),
                    "spotify_outcome": "not_added",
                },
                status_code=503,
            )
        headers = retry_after(view, now)
        if view["status"] == "added":
            # The receipt Cochlea 0.5.0 already treats as delivered.
            return JSONResponse({"ok": True, **view})
        if view["status"] == "not_added":
            message = f"Could not find {view['title']} by {view['artist']} on Spotify"
            return JSONResponse(
                {"ok": False, **view, "message": message}, status_code=422, headers=headers
            )
        if view["status"] == "rejected":
            return JSONResponse({"ok": False, **view}, status_code=403)
        if due(view, now):
            background.add_task(_spawn_drain)
        return JSONResponse({"ok": True, **view}, status_code=202, headers=headers)

    @api.get("/{capture_id}")
    def status(
        capture_id: str,
        background: BackgroundTasks,
        authorization: Annotated[str | None, Header()] = None,
    ):
        try:
            client_id = client_for(authorization)
        except capture_clients.Unauthorized:
            return unauthorized()
        except Exception:
            return JSONResponse({"ok": False, "message": "capture unavailable"}, status_code=503)
        try:
            capture_id = str(UUID(capture_id))
        except ValueError:
            return JSONResponse({"ok": False, "message": "unknown capture"}, status_code=404)
        try:
            view = capture_clients.status(Settings(), client_id, capture_id)
        except Exception:
            return JSONResponse({"ok": False, "message": "capture unavailable"}, status_code=503)
        if view is None:
            return JSONResponse({"ok": False, "message": "unknown capture"}, status_code=404)
        if due(view, datetime.now(timezone.utc)):
            background.add_task(_spawn_drain)
        return view

    return api


@app.function(image=image)
@modal.fastapi_endpoint(method="GET", label="capture-enroll")
def capture_enroll():
    """Static page an enrollment link opens; it forwards to offlineshazam://enroll."""
    from fastapi.responses import HTMLResponse

    from core.enroll_page import ENROLL_PAGE

    return HTMLResponse(ENROLL_PAGE, headers={"Cache-Control": "no-store"})


@app.function(image=image, timeout=WORKER_TIMEOUT)
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


def _deliver_queued(client_id: str, payload: dict, received_at: str):
    """The drain's step: the synchronous select/add/catalog delivery of one capture."""
    import gzip
    import json
    from datetime import datetime

    from core import archive, capture_clients, workspaces
    from core import capture as cap
    from core.config import Settings

    base = Settings()
    capture_clients.require_active(base, client_id)
    # The client's workspace decides whose Spotify, hub and Notion this touches.
    settings = workspaces.settings_for(base, capture_clients.client_workspace(base, client_id))
    # A pending reconcile, replay or package checkpoint goes first: wait before any
    # Spotify call (the drain keeps the capture queued and stops).
    pending = archive.get(settings, archive.pending_key(settings))
    if pending and json.loads(gzip.decompress(pending)):
        return {
            "ok": False,
            "message": "Pending recovery; retry after the original operation completes",
        }
    received = datetime.fromisoformat(received_at.replace("Z", "+00:00"))
    return capture_clients.deliver(
        settings,
        client_id,
        payload,
        lambda body: cap.resolve_track(body, None, settings),
        lambda body, selected, record: _capture(
            body, selected, settings, record, client_id=client_id, now=received, mirror_cache=False
        ),
    )


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
    if action == "remove":
        return {"removed": workspaces.remove(base, body["workspace"])}
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


def _settings(body: dict):
    from core import workspaces
    from core.config import Settings

    return workspaces.settings_for(Settings(), body.get("workspace") or "default")


def _observe(body: dict):
    """Observation import (no Spotify writes); allowed while RECONCILE_ENABLED=0.

    `replan: true` discards a pending observation-only import so it is planned again
    from current state; it never discards a mutation, replay or package checkpoint.
    """
    import gzip
    import json

    from core import archive, run

    settings = _settings(body)
    if body.get("replan") is True:
        pending = _pending(settings)
        if pending and (pending.get("intent") or pending.get("writes") is not False):
            return {
                "applied": {},
                "flags": [],
                "errors": ["pending recovery is not an observation-only import"],
            }
        if pending:
            archive.put(
                settings, archive.pending_key(settings), gzip.compress(json.dumps(None).encode())
            )
    log = run.reconcile(settings, dry_run=False, writes=False, deadline=_budget())
    return {"applied": dict(log.applied), "flags": log.flags, "errors": log.errors}


def _preview(body: dict):
    """Gzipped full dry-run receipt; with `package`, the first run after that package."""
    import gzip
    import json
    from dataclasses import asdict

    from core import run

    log = run.reconcile(
        _settings(body),
        dry_run=True,
        package=body.get("package"),
        observation_key=body.get("observation_key"),
    )
    return gzip.compress(json.dumps(asdict(log), ensure_ascii=False, allow_nan=False).encode())


def _pending(settings):
    import gzip
    import json

    from core import archive

    saved = archive.get(settings, archive.pending_key(settings))
    return json.loads(gzip.decompress(saved)) if saved else None


def _package(body: dict):
    """Apply one confirmed rollout package; a crash resumes from the retained checkpoint."""
    import gzip
    import json
    from datetime import datetime, timezone

    from core import archive, metadata
    from core import package as pkg
    from core.hub import Hub, with_read_retries
    from core.spotify_client import SpotifyClient

    settings = _settings(body)
    doc = body.get("package")
    if not isinstance(doc, dict) or body.get("confirm") != pkg.digest(doc):
        return {"applied": False, "problems": ["confirm must equal the package digest"]}
    pending = _pending(settings)
    if pending and (pending.get("intent") != "package" or pending.get("digest") != body["confirm"]):
        return {"applied": False, "problems": ["another pending recovery must finish first"]}
    state = pending["state"] if pending else {}
    key = archive.pending_key(settings)

    def save():
        retained = {"intent": "package", "digest": body["confirm"], "state": state}
        archive.put(settings, key, gzip.compress(json.dumps(retained).encode()))

    hub = with_read_retries(Hub(settings.soma_hub_url, settings.soma_hub_token))
    metadata.require_observed_contract(hub)
    receipt = pkg.apply(
        doc, SpotifyClient(settings), hub, settings, datetime.now(timezone.utc), state, save
    )
    if not receipt.get("rate_limited"):  # a rate-limited stop resumes from this checkpoint
        archive.put(settings, key, gzip.compress(json.dumps(None).encode()))
    return receipt


def _rules(body: dict):
    from datetime import date

    from fastapi.responses import JSONResponse

    from core import mirror, smart
    from core.hub import Hub, with_read_retries
    from core.spotify_client import SpotifyClient

    settings = _settings(body)
    hub = with_read_retries(Hub(settings.soma_hub_url, settings.soma_hub_token))
    m = mirror.load_playlists(hub)
    if body.get("action") == "list":
        return smart.listing(m)
    if _pending(settings):
        return JSONResponse({"ok": False, "message": "pending recovery"}, status_code=409)
    spotify = SpotifyClient(settings)
    me = spotify.me()["id"]
    owned = {
        p["id"]: p["name"]
        for p in spotify.get_playlists()
        if (p.get("owner") or {}).get("id") == me
    }
    try:
        return smart.configure(
            body,
            hub=hub,
            mirror=m,
            live_names=owned,
            today=date.today().isoformat(),
            spotify=spotify,
        )
    except smart.ConfigError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=422)


def _recognitions(body: dict):
    """Create-only import of past recognitions plus the songs Shazam projection."""
    from datetime import datetime, timezone

    from core import metadata, recognition
    from core.hub import Hub, with_read_retries

    settings = _settings(body)
    if _pending(settings):
        return {"ok": False, "message": "pending recovery"}
    hub = with_read_retries(Hub(settings.soma_hub_url, settings.soma_hub_token))
    legacy = hub.pull(
        "provenance",
        ["id", "from_ref", "to_ref", "created_at", "deleted_at"],
        where={"from_kind": "shazam", "to_kind": "songs", "rel": "imported_from"},
    )
    events = list(body.get("historical") or []) + recognition.legacy_capture_events(legacy)
    songs = {r["id"] for r in hub.pull("songs", ["id", "deleted_at"]) if not r.get("deleted_at")}
    missing = sorted({(e["isrc"] or "").strip().upper() for e in events} - songs)
    if body.get("dry_run", True):
        return {
            "dry_run": True,
            "events": len(events),
            "legacy": len(events) - len(body.get("historical") or []),
            "missing_songs": missing,
        }
    columns = metadata.require_observed_contract(hub)
    receipt = recognition.import_history(settings, hub, events, datetime.now(timezone.utc))
    if not set(recognition.PROJECTION) <= columns:
        receipt.pop("known")
        return {**receipt, "projected": 0, "projection": "catalog columns missing"}
    rows = hub.pull(
        "provenance",
        recognition.PROV_COLS,
        where={"to_kind": "songs", "rel": "evidence_of", "asserted_by": "music-sync"},
    )
    projected = [
        r for r in recognition.projections(settings, rows, receipt.pop("known")) if r["id"] in songs
    ]
    hub.push("songs", projected)
    return {**receipt, "projected": len(projected), "missing_songs": missing}


ROLLOUT = {
    "observe": _observe,
    "preview": _preview,
    "package": _package,
    "rules": _rules,
    "recognitions": _recognitions,
}


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


def _capture(
    body: dict,
    resolved_track: dict | None = None,
    settings=None,
    record_outcome=None,
    client_id: str | None = None,
    now=None,
    mirror_cache: bool = True,
):
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
            now or datetime.now(timezone.utc),
            resolved_track=resolved_track,
            record_outcome=record_outcome,
            client_id=client_id,
            use_cache=mirror_cache,
        )
    except SpotifyAuthError as e:
        _flag_quietly(
            s,
            [],
            [
                f"Spotify refresh token {e}: reconnect with `just workspace connect-link {s.workspace}`"
            ],
        )
        if record_outcome is not None:
            raise
        return JSONResponse(
            {"ok": False, "message": "Spotify token expired; flagged"}, status_code=503
        )
    if not out["ok"] and out["message"].startswith("Could not find"):
        _flag_quietly(
            s, [f"Add {body.get('title')} by {body.get('artist')} to new songs manually"], []
        )
    return out
