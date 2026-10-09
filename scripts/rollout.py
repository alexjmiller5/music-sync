"""One-time rollout operations (operator tool). Receipts hold personal catalog data:
write them to a private state directory, never into the repo.

    uv run scripts/rollout.py observe --output <receipt.json>
    uv run scripts/rollout.py build --receipt <preview.json> --decisions <decisions.json> \\
        --spec <spec.json> --output <package.json>
    uv run scripts/rollout.py apply --package <package.json> --confirm <digest> --output <receipt.json>
    uv run scripts/rollout.py recognitions --manifest <history.json> --output <receipt.json> [--apply]

`observe` imports a complete observation without Spotify writes (allowed while
reconciliation is disabled). `build` is local and pure. `apply` runs the confirmed
package in the serialized worker; rerunning the same package resumes it.
`recognitions` imports past Shazam recognitions with explicitly estimated dates.
Remote commands use the operator's Modal auth.
"""

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

APP_NAME = "music-sync"


def private_write(path: Path, data: bytes) -> str:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
    return hashlib.sha256(data).hexdigest()


def dump(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()


def worker():
    import modal

    return modal.Function.from_name(APP_NAME, "worker")


def build(args) -> dict:
    from core import package
    from core.mirror import live_from_raw
    from core.model import Membership, Mirror, Playlist, Song

    receipt = json.loads(args.receipt.read_text())
    snap = receipt["snapshot"]
    data = snap["mirror"]
    mirror = Mirror(
        {s["id"]: Song(**s) for s in data["songs"]},
        {p["id"]: Playlist(**p) for p in data["playlists"]},
        {(m["playlist_id"], m["isrc"]): Membership(**m) for m in data["memberships"]},
        [Membership(**m) for m in data["deleted_memberships"]],
        {tuple(pair) for pair in data["captures"]},
    )
    live = live_from_raw(snap["spotify"], snap["me"], mirror)
    out = package.build(
        live,
        mirror,
        json.loads(args.decisions.read_text()),
        json.loads(args.spec.read_text()),
        observed_at=snap["observed_at"],
    )
    out["digest"] = package.digest(out["package"])
    out["receipt_sha256"] = hashlib.sha256(args.receipt.read_bytes()).hexdigest()
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    observe = sub.add_parser("observe")
    observe.add_argument("--output", type=Path, required=True)
    observe.add_argument(
        "--replan", action="store_true", help="replace a pending observation-only import"
    )
    b = sub.add_parser("build")
    for name in ("receipt", "decisions", "spec", "output"):
        b.add_argument(f"--{name}", type=Path, required=True)
    a = sub.add_parser("apply")
    a.add_argument("--package", type=Path, required=True)
    a.add_argument("--confirm", required=True)
    a.add_argument("--output", type=Path, required=True)
    r = sub.add_parser("recognitions")
    r.add_argument("--manifest", type=Path, required=True)
    r.add_argument("--output", type=Path, required=True)
    r.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "observe":
        result = worker().remote("observe", {"replan": True} if args.replan else {})
    elif args.command == "build":
        result = build(args)
        summary = {
            "digest": result["digest"],
            "playlists_edited": len(result["package"]["playlists"]),
            "likes": len(result["package"]["likes"]),
            "unlikes": len(result["package"]["unlikes"]),
            "held": len(result["report"]["held"]),
            "stale": len(result["report"]["stale"]),
        }
        sha = private_write(args.output, dump(result))
        print(json.dumps({**summary, "output": str(args.output), "sha256": sha}))
        return 0
    elif args.command == "apply":
        doc = json.loads(args.package.read_text())
        doc = doc.get("package", doc)
        result = worker().remote("package", {"package": doc, "confirm": args.confirm})
    else:
        manifest = json.loads(args.manifest.read_text())
        events = [
            {
                "evidence_id": e["evidence_id"],
                "isrc": e["source_isrc"],
                "estimated_recognized_at": e["estimated_shazam_at"],
                "estimate_basis": e["date_basis"],
                "source_ref": e["archive_key"],
                "source_locator": e["locator"],
            }
            for e in manifest["events"]
            if e.get("recognized_at") is None and e.get("shazamed")
        ]
        result = worker().remote("recognitions", {"historical": events, "dry_run": not args.apply})
    sha = private_write(args.output, dump(result))
    print(json.dumps({"output": str(args.output), "sha256": sha}))
    errors = result.get("errors") or result.get("problems") if isinstance(result, dict) else None
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
