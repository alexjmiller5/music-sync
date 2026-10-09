"""One reconcile run, end to end. Plain Python; app.py calls this."""

import gzip
import json
from dataclasses import asdict
from datetime import datetime, timezone

import httpx
import structlog

from core import actions, archive, curation, flags, history, mirror, metadata
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
    package: dict | None = None,
) -> actions.RunLog:
    """`package` (dry runs only) previews the first reconciliation after that package."""
    if package is not None and not dry_run:
        raise ValueError("a package is applied only through the package operation")
    now = now or datetime.now(timezone.utc)
    today = now.date().isoformat()
    saved = archive.get(settings, archive.pending_key(settings))
    pending = json.loads(gzip.decompress(saved)) if saved else None
    if pending and pending.get("intent") == "metadata_replay":
        return actions.RunLog(
            dry_run=dry_run,
            errors=["Pending metadata replay recovery must finish through metadata_replay"],
        )
    if pending and pending.get("intent") == "package":
        return actions.RunLog(
            dry_run=dry_run,
            errors=["Pending rollout package must finish through the package operation"],
        )
    if pending and dry_run:
        return actions.RunLog(
            dry_run=True,
            errors=[
                "Activation preview blocked by pending recovery; "
                "complete recovery, then request a fresh dry run"
            ],
        )
    if pending and pending.get("writes") and pending.get("policy_version") != 1:
        return actions.RunLog(
            dry_run=dry_run,
            errors=[
                "Pending reconciliation uses an older curation policy; review retained intent before recovery"
            ],
        )
    if pending and pending["writes"] and not writes:
        return actions.RunLog(
            errors=["Pending reconcile recovery must finish before observation import"]
        )
    http = http or httpx.Client(timeout=60)
    spotify = spotify or SpotifyClient(settings)
    hub = hub or Hub(settings.life_hub_url, settings.life_hub_token)
    if not dry_run:
        metadata.require_observed_contract(hub)
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
    if pending:
        if not dry_run:
            live = mirror.pull_live(
                spotify, settings.spotify_market, me, Mirror({}, {}, {}, [], set()), full=True
            )
            archive.put(
                settings, archive.key_for(now), gzip.compress(json.dumps(live.raw).encode())
            )
        plan = [Action(**a) for a in pending["planned"]]
        next_curation = pending.get("curation_state")
    else:
        m = mirror.load_mirror(hub)
        live = mirror.pull_live(spotify, settings.spotify_market, me, m, full=True)
        source_ref = archive.key_for(now)
        if not dry_run:
            archive.put(settings, source_ref, gzip.compress(json.dumps(live.raw).encode()))
        retained = archive.get(settings, curation.state_key(settings.workspace))
        previous = json.loads(gzip.decompress(retained)) if retained else None
        observed_raw = live.raw
        if package is not None:
            from core import package as package_mod

            problems = package_mod.validate(package, live, {})
            if problems:
                return actions.RunLog(dry_run=True, errors=problems)
            if previous is None:
                _, previous = curation.advance(None, set(live.liked), _curated(m, live), source_ref)
            m, live = package_mod.simulate(package, m, live, me, now)
            previous = package_mod.adjust_curation(
                previous,
                package,
                live,
                {pid: p.kind for pid, p in m.playlists.items()},
                {u["isrc"] for u in package["unlikes"]},
            )
        members = _curated(m, live)
        if any(playlist.items is None for playlist in live.playlists.values()):
            return actions.RunLog(dry_run=dry_run, errors=["Incomplete curation observation"])
        to_like, next_curation = curation.advance(previous, set(live.liked), members, source_ref)
        plan = reconcile_mod.plan(
            m,
            live,
            now,
            settings.inbox_cap,
            settings.undo_days,
            today,
            observation_only=not writes,
            source_ref=source_ref,
            market=settings.spotify_market,
            curation_likes=to_like,
        )
        occurrence_actions, fingerprints = history.occurrence_evidence(
            live.raw, source_ref, (previous or {}).get("occurrence_fingerprints", {})
        )
        plan = [*plan, *occurrence_actions]
        next_curation["occurrence_fingerprints"] = fingerprints
        confirmed_on_success = {a.isrc for a in plan if a.kind == "like"} if writes else set()
        next_curation["baseline"]["own_likes"] = sorted(confirmed_on_success)
        next_curation["pending_likes"] = sorted(
            set(next_curation["pending_likes"]) - confirmed_on_success
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
        if not remaining and next_curation is not None:
            archive.put(
                settings,
                curation.state_key(settings.workspace),
                gzip.compress(json.dumps(next_curation).encode()),
            )
        data = (
            {
                "policy_version": 1,
                "curation_state": next_curation,
                "planned": [asdict(a) for a in plan],
                "operations": remaining,
                "writes": pending["writes"] if pending else writes,
            }
            if remaining
            else None
        )
        archive.put(
            settings, archive.pending_key(settings), gzip.compress(json.dumps(data).encode())
        )

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
    if dry_run and not pending:
        out.snapshot = {
            "observed_at": now.isoformat(),
            "market": settings.spotify_market,
            "retained_remotely": False,
            "atomic": False,
            "me": me,
            "spotify": observed_raw,
            "package": None
            if package is None
            else {"digest": package_mod.digest(package), "spotify_after": live.raw},
            "revisions": m.revisions,
            "mirror": {
                "observations": m.observations,
                "captures": [list(pair) for pair in sorted(m.captures)],
                "songs": [asdict(row) for row in m.songs.values()],
                "playlists": [asdict(row) for row in m.playlists.values()],
                "memberships": [asdict(row) for row in m.memberships.values()],
                "deleted_memberships": [asdict(row) for row in m.deleted_memberships],
            },
            "migration_candidates": curation.bootstrap_candidates(
                set(live.liked), members, next_curation["exceptions"]
            ),
            "curation_before": previous,
            "curation_after_if_applied": next_curation,
            "planner_settings": {"inbox_cap": settings.inbox_cap, "undo_days": settings.undo_days},
        }
    names = {p.id: p.name for p in m.playlists.values()} if not pending else {}
    out.review_items = [
        {
            "id": f"curation-unlike:{isrc}",
            "isrc": isrc,
            "title": m.songs[isrc].title if not pending and isrc in m.songs else None,
            "playlists": sorted(
                names.get(pid, pid)
                for pid, i in (next_curation or {}).get("baseline", {}).get("curated", [])
                if i == isrc
            ),
            **item,
        }
        for isrc, item in (next_curation or {}).get("exceptions", {}).items()
    ]
    log.info(
        "reconcile_done",
        **{k: v for k, v in out.applied.items() if v},
        flags=len(out.flags),
        errors=len(out.errors),
    )
    if not dry_run and writes:
        # Observation imports return their flags to the operator for triage instead.
        flags.file(settings, http, out.flags, out.errors, today)
        if not out.errors:
            from core.life_flags import deliver_reviews

            deliver_reviews(settings, http, out.review_items, today)
    return out


def _curated(m: Mirror, live) -> set[tuple[str, str]]:
    return {
        (pid, item.isrc)
        for pid, playlist in live.playlists.items()
        if pid in m.playlists and m.playlists[pid].kind == "curated"
        for item in playlist.items or []
        if item.isrc
    }


reconcile = reconcile_run  # name used by app.py and tests: run.reconcile(...)
