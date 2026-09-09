"""Modal deployment shim - ALL infrastructure lives here, as code.

Business logic stays in src/core/ (plain Python, no Modal imports). This file
maps it onto Modal: image, secrets, the hourly reconcile, and two proxy-auth
endpoints (/reconcile on demand, /capture for the Shazam shortcut).
"""

import os

import modal

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
    if operation != "reconcile":
        raise ValueError("unknown operation")
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


def _capture(body: dict):
    from datetime import datetime, timezone

    from fastapi.responses import JSONResponse

    from core import capture as cap
    from core.config import Settings
    from core.hub import Hub
    from core.spotify_client import SpotifyAuthError, SpotifyClient

    s = Settings()
    try:
        out = cap.capture(
            body or {},
            SpotifyClient(s),
            Hub(s.life_hub_url, s.life_hub_token),
            s,
            datetime.now(timezone.utc),
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
