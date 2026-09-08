# /// script
# requires-python = ">=3.13"
# dependencies = ["httpx", "pydantic-settings", "structlog"]
# ///
"""One-time migration steps (spec section 9). Each step prints its plan and
stops there unless --dry-run is ABSENT - i.e. --dry-run means print only.

    op run --env-file=.env.tpl -- uv run scripts/migrate.py --step <name> [--dry-run] [--playlist-id P] [--yes]

Steps: inbox, first-pull, shazam-edges, like-pool, smart-buckets, rules, enable.
"""

import argparse
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

STEPS = ["inbox", "first-pull", "shazam-edges", "like-pool", "smart-buckets", "rules", "enable"]
BUCKET_RULES = {
    "the good stuff": {"v": 1, "first_year": {"gte": 2000}},
    "galaxy": {"v": 1, "first_year": {"lt": 2000}},
    "rap": {"v": 1, "deezer_genres_any": ["Rap/Hip Hop"]},
}
RULE_NAME = "smart-songs-match-rule"
RULE_TEXT = "Every song in a smart playlist matches its rule (reconciler bug if violated)."


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--step", required=True, choices=STEPS)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--playlist-id", help="Spotify playlist id (inbox step)")
    p.add_argument(
        "--yes", action="store_true", help="required to actually like the pool (like-pool step)"
    )
    return p.parse_args(argv)


def step_inbox(hub, playlist_id: str | None, dry_run: bool) -> None:
    if not playlist_id:
        sys.exit("inbox step requires --playlist-id")
    row = {
        "id": playlist_id,
        "name": "new songs",
        "kind": "inbox",
        "rule": None,
        "description": None,
        "snapshot_id": None,
        "pinned": 0,
        "expires_at": None,
        "deleted_at": None,
    }
    print(f"inbox: push playlists row {row}")
    if dry_run:
        return
    hub.push("playlists", [row])
    print("done")


def step_first_pull(settings, dry_run: bool) -> None:
    print(
        "first-pull: run.reconcile(dry_run=False, writes=False) - hub rows + archive only, no Spotify writes"
    )
    if dry_run:
        return
    from core import run

    log = run.reconcile(settings, dry_run=False, writes=False)
    print(log.summary())


def step_shazam_edges(hub, dry_run: bool) -> None:
    from core import mirror as mm

    m = mm.load_mirror(hub)
    pid = next((p.id for p in m.playlists.values() if p.name == "My Shazam Tracks"), None)
    if pid is None:
        sys.exit("no 'My Shazam Tracks' playlist in the mirror")
    isrcs = sorted(isrc for (q, isrc) in m.memberships if q == pid)
    print(f"shazam-edges: push {len(isrcs)} provenance edges for My Shazam Tracks")
    if dry_run:
        return
    rows = [
        {
            "id": f"shazam:{i}:{i}",
            "from_kind": "shazam",
            "from_ref": i,
            "to_kind": "songs",
            "to_ref": i,
            "rel": "imported_from",
            "asserted_by": "music-sync",
            "detail": {"created_row": 0},
        }
        for i in isrcs
    ]
    hub.push("provenance", rows)
    print("done")


def step_like_pool(hub, spotify, yes: bool, dry_run: bool) -> None:
    from core import mirror as mm

    m = mm.load_mirror(hub)
    names = {"the good stuff", "galaxy", "rap"}
    pids = {p.id for p in m.playlists.values() if p.name in names}
    isrcs = {isrc for (pid, isrc) in m.memberships if pid in pids}
    to_like = [
        m.songs[i]
        for i in sorted(isrcs)
        if i in m.songs and not m.songs[i].liked and m.songs[i].spotify_ids
    ]
    print(f"like-pool: {len(to_like)} unliked songs across {sorted(names)}")
    if dry_run:
        return
    if not yes:
        print("refusing to like without --yes")
        return
    spotify.like([f"spotify:track:{s.spotify_ids[0]}" for s in to_like])
    print("done")


def step_smart_buckets(hub, dry_run: bool) -> None:
    from core import mirror as mm

    m = mm.load_mirror(hub)
    rows = []
    for name, rule in BUCKET_RULES.items():
        p = next((p for p in m.playlists.values() if p.name == name), None)
        if p is None:
            sys.exit(f"no '{name}' playlist in the mirror")
        rows.append({"id": p.id, "kind": "smart", "rule": rule})
    print(f"smart-buckets: set kind=smart + rule on {sorted(r['id'] for r in rows)}")
    if dry_run:
        return
    hub.push("playlists", rows)
    print("done")


def step_rules(hub, dry_run: bool) -> list[str]:
    from core import mirror as mm
    from core import rules

    m = mm.load_mirror(hub)
    ids = {p.name: p.id for p in m.playlists.values()}
    smart = [p for p in m.playlists.values() if p.kind == "smart"]
    sql = rules.check_sql(smart, ids).replace(
        "FROM captures c WHERE c.isrc = s.id AND c.from_kind",
        "FROM provenance c WHERE c.to_kind='songs' AND c.rel='imported_from' AND c.deleted_at IS NULL "
        "AND c.to_ref = s.id AND c.from_kind",
    )
    cmd = [
        "life",
        "rule",
        "set",
        RULE_NAME,
        "--scope",
        "table",
        "--tbl",
        "playlist_songs",
        "--kind",
        "invariant",
        "--text",
        RULE_TEXT,
        "--sql",
        sql,
    ]
    print("rules: " + " ".join(shlex.quote(c) for c in cmd))
    if dry_run:
        return cmd
    subprocess.run(cmd, check=True)
    print("done")
    return cmd


def step_enable() -> None:
    print(
        "enable: RECONCILE_ENABLED=1 is your approval - run these yourself, never automatically:\n"
    )
    print("op item edit 'Music Sync ENV' --vault 'Music Sync' RECONCILE_ENABLED=1")
    print("just sync-secrets")


def main() -> None:
    args = parse_args()
    if args.step == "enable":
        step_enable()
        return
    from core.config import Settings
    from core.hub import Hub

    settings = Settings()
    hub = Hub(settings.life_hub_url, settings.life_hub_token)
    match args.step:
        case "inbox":
            step_inbox(hub, args.playlist_id, args.dry_run)
        case "first-pull":
            step_first_pull(settings, args.dry_run)
        case "shazam-edges":
            step_shazam_edges(hub, args.dry_run)
        case "like-pool":
            from core.spotify_client import SpotifyClient

            step_like_pool(hub, SpotifyClient(settings), args.yes, args.dry_run)
        case "smart-buckets":
            step_smart_buckets(hub, args.dry_run)
        case "rules":
            step_rules(hub, args.dry_run)


if __name__ == "__main__":
    main()
