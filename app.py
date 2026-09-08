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
        "flags": log.flags,
        "errors": log.errors,
    }


@app.function(
    image=image, secrets=secrets, schedule=modal.Cron("0 * * * *"), max_containers=1, timeout=1500
)
def reconcile_cron():
    # Activation gate (spec req 18): the migration flips RECONCILE_ENABLED=1 in the
    # Modal secret after the manual review and a clean dry-run.
    if os.environ.get("RECONCILE_ENABLED") != "1":
        print("reconcile_cron: RECONCILE_ENABLED != 1, skipping")
        return {"skipped": True}
    return _run(dry_run=False)


@app.function(image=image, secrets=secrets, max_containers=1, timeout=1500)
@modal.fastapi_endpoint(method="POST", requires_proxy_auth=True)
def reconcile(body: dict | None = None):
    return _run(dry_run=bool((body or {}).get("dry_run", False)))


@app.function(image=image, secrets=secrets, timeout=120)
@modal.fastapi_endpoint(method="POST", requires_proxy_auth=True)
def capture(body: dict):
    from datetime import date, datetime, timezone

    import httpx
    from fastapi.responses import JSONResponse

    from core import capture as cap
    from core import flags
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
        flags.file(
            s,
            httpx.Client(),
            [],
            [f"Spotify refresh token {e}: re-mint with scripts/spotify_auth.py"],
            date.today().isoformat(),
        )
        return JSONResponse(
            {"ok": False, "message": "Spotify token expired; flagged"}, status_code=503
        )
    if not out["ok"] and out["message"].startswith("Could not find"):
        flags.file(
            s,
            httpx.Client(),
            [f"Add {body.get('title')} by {body.get('artist')} to new songs manually"],
            [],
            date.today().isoformat(),
        )
    return out
