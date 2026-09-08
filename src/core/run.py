"""One reconcile run, end to end. Plain Python; app.py calls this."""

import gzip
import json
from datetime import datetime, timezone

import httpx
import structlog

from core import actions, archive, flags, mirror
from core import reconcile as reconcile_mod
from core.config import Settings
from core.hub import Hub
from core.spotify_client import SpotifyAuthError, SpotifyClient

log = structlog.get_logger()


def reconcile_run(
    settings: Settings,
    dry_run: bool = False,
    now: datetime | None = None,
    spotify=None,
    hub=None,
    http: httpx.Client | None = None,
    writes: bool = True,
) -> actions.RunLog:
    now = now or datetime.now(timezone.utc)
    today = now.date().isoformat()
    http = http or httpx.Client(timeout=60)
    spotify = spotify or SpotifyClient(settings)
    hub = hub or Hub(settings.life_hub_url, settings.life_hub_token)
    try:
        me = spotify.me()["id"]
    except SpotifyAuthError as e:
        out = actions.RunLog(
            dry_run=dry_run,
            errors=[f"Spotify refresh token {e}: re-mint with scripts/spotify_auth.py"],
        )
        flags.file(settings, http, [], out.errors, today)
        return out
    m = mirror.load_mirror(hub)
    live = mirror.pull_live(spotify, settings.spotify_market, me, m)
    if not dry_run:
        archive.put(settings, archive.key_for(now), gzip.compress(json.dumps(live.raw).encode()))
    plan = reconcile_mod.plan(m, live, now, settings.inbox_cap, settings.undo_days, today)
    out = actions.apply(plan, spotify, hub, dry_run, writes=writes)
    log.info(
        "reconcile_done",
        **{k: v for k, v in out.applied.items() if v},
        flags=len(out.flags),
        errors=len(out.errors),
    )
    if not dry_run:
        flags.file(settings, http, out.flags, out.errors, today)
    return out


reconcile = reconcile_run  # name used by app.py and tests: run.reconcile(...)
