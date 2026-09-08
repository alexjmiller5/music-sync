# /// script
# requires-python = ">=3.13"
# dependencies = ["httpx", "pydantic-settings", "structlog"]
# ///
"""Non-compliance report over the mirror (spec section 9.7). Read-only.

op run --env-file=.env.tpl -- uv run scripts/review.py [--json]
"""

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.model import Live, Mirror  # noqa: E402

BUCKETS = {
    "the good stuff": lambda s: (s.first_year or 0) >= 2000,
    "galaxy": lambda s: (s.first_year or 9999) < 2000,
    "rap": lambda s: "Rap/Hip Hop" in (s.deezer_genres or []),
}
_NORM = re.compile(r"\s*[\(\[\-].*$")


def _line(s, m: Mirror) -> str:
    where = sorted(
        m.playlists[pid].name
        for (pid, isrc) in m.memberships
        if isrc == s.id and pid in m.playlists
    )
    return f"- {s.title} - {', '.join(s.artists)} [{s.id}] ({s.first_year}) in: {', '.join(where) or '-'}"


def report(m: Mirror, live: Live | None) -> dict[str, list[str]]:
    r = defaultdict(list)
    by_name = {p.name: p.id for p in m.playlists.values()}
    kind = {p.id: p.kind for p in m.playlists.values()}
    in_playlist = {isrc for (_, isrc) in m.memberships}
    for (pid, isrc), _ in m.memberships.items():
        s = m.songs.get(isrc)
        if s and kind.get(pid) == "curated" and not s.liked:
            r["curated_not_liked"].append(_line(s, m))
    for s in m.songs.values():
        if s.liked and s.id not in in_playlist:
            r["liked_no_playlist"].append(_line(s, m))
    for name, ok in BUCKETS.items():
        pid = by_name.get(name)
        for q, isrc in m.memberships:
            if q == pid and isrc in m.songs and not ok(m.songs[isrc]):
                r["bucket_mismatch"].append(_line(m.songs[isrc], m) + f" <- {name}")
    if live:
        for lp in live.playlists.values():
            seen = Counter()
            for it in lp.items or []:
                if not it.isrc:
                    r["no_isrc"].append(
                        f"{lp.name}: {it.name} ({'local file' if it.is_local else 'no ISRC'})"
                    )
                    continue
                seen[it.isrc] += 1
                s = m.songs.get(it.isrc)
                if not it.playable and s:
                    alt = s.spotify_playable and s.spotify_ids and s.spotify_ids[0] != it.track_id
                    r["unplayable_alt" if alt else "unplayable_none"].append(
                        _line(s, m) + f" <- {lp.name}"
                    )
            for isrc, n in seen.items():
                if n > 1 and isrc in m.songs:
                    r["dup_isrc_in_playlist"].append(
                        _line(m.songs[isrc], m) + f" x{n} in {lp.name}"
                    )
    groups = defaultdict(list)
    for s in m.songs.values():
        groups[(_NORM.sub("", s.title or "").casefold(), (s.artists or [""])[0].casefold())].append(
            s
        )
    for (title, artist), ss in groups.items():
        if len(ss) > 1 and title:
            r["same_title_diff_isrc"].append(
                f"- {ss[0].title} - {artist}: "
                + "; ".join(f"{s.title} [{s.id}] ({s.first_year})" for s in ss)
            )
    shz = by_name.get("My Shazam Tracks")
    for pid, isrc in m.memberships:
        if pid == shz and isrc in m.songs and not m.songs[isrc].liked:
            r["shazam_never_liked"].append(_line(m.songs[isrc], m))
    return {k: sorted(v) for k, v in r.items()} | {
        k: []
        for k in [
            "curated_not_liked",
            "liked_no_playlist",
            "bucket_mismatch",
            "unplayable_alt",
            "unplayable_none",
            "dup_isrc_in_playlist",
            "same_title_diff_isrc",
            "shazam_never_liked",
            "no_isrc",
        ]
        if k not in r
    }


def main() -> None:
    from core import mirror as mm
    from core.config import Settings
    from core.hub import Hub
    from core.spotify_client import SpotifyClient

    s = Settings()
    sp = SpotifyClient(s)
    m = mm.load_mirror(Hub(s.life_hub_url, s.life_hub_token))
    live = mm.pull_live(
        sp, s.spotify_market, sp.me()["id"], Mirror({}, {}, {}, [], set())
    )  # full pull, no snapshot skipping
    out = report(m, live)
    if "--json" in sys.argv:
        print(json.dumps(out, indent=1, ensure_ascii=False))
        return
    for k, lines in out.items():
        print(f"\n## {k} ({len(lines)})")
        print("\n".join(lines))


if __name__ == "__main__":
    main()
