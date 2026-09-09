"""One reconcile run, end to end. Plain Python; app.py calls this."""

import gzip
import json
from dataclasses import asdict
from datetime import datetime, timezone

import httpx
import structlog

from core import actions, archive, flags, mirror
from core import reconcile as reconcile_mod
from core.config import Settings
from core.hub import Hub
from core.model import Action, Mirror
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
        if not dry_run:
            flags.file(settings, http, [], out.errors, today)
        return out
    saved = archive.get(settings, archive.PENDING_KEY)
    pending = json.loads(gzip.decompress(saved)) if saved else None
    if pending and dry_run:
        return actions.RunLog(
            dry_run=True,
            errors=[
                "Activation preview blocked by pending recovery; "
                "complete recovery, then request a fresh dry run"
            ],
        )
    if pending and pending["writes"] and not writes:
        return actions.RunLog(
            errors=["Pending reconcile recovery must finish before observation import"]
        )
    if pending:
        if not dry_run:
            live = mirror.pull_live(
                spotify, settings.spotify_market, me, Mirror({}, {}, {}, [], set()), full=True
            )
            archive.put(
                settings, archive.key_for(now), gzip.compress(json.dumps(live.raw).encode())
            )
        plan = [Action(**a) for a in pending["planned"]]
    else:
        m = mirror.load_mirror(hub)
        live = mirror.pull_live(spotify, settings.spotify_market, me, m, full=not writes)
        if not dry_run:
            archive.put(
                settings, archive.key_for(now), gzip.compress(json.dumps(live.raw).encode())
            )
        plan = reconcile_mod.plan(
            m,
            live,
            now,
            settings.inbox_cap,
            settings.undo_days,
            today,
            observation_only=not writes,
        )
        for a in plan:
            lp = live.playlists.get(a.playlist_id)
            mp = m.playlists.get(a.playlist_id)
            a.playlist_name = lp.name if lp else mp.name if mp else None
            item = live.liked.get(a.isrc) or next(
                (
                    it
                    for p in live.playlists.values()
                    for it in (p.items or [])
                    if it.isrc == a.isrc
                ),
                None,
            )
            song = m.songs.get(a.isrc)
            a.title = item.name if item else song.title if song else None

    def checkpoint(remaining):
        data = (
            {
                "planned": [asdict(a) for a in plan],
                "operations": remaining,
                "writes": pending["writes"] if pending else writes,
            }
            if remaining
            else None
        )
        archive.put(settings, archive.PENDING_KEY, gzip.compress(json.dumps(data).encode()))

    out = actions.apply(
        plan,
        spotify,
        hub,
        dry_run,
        writes=writes,
        checkpoint=checkpoint,
        pending=pending["operations"] if pending else None,
        market=settings.spotify_market,
    )
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
