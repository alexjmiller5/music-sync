"""Read-only rollout preview through the owning app's normal configuration.

uv run scripts/preview.py --output /private/state/music-preview.json
The receipt contains personal catalog data and must stay outside source control.
"""

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core import run  # noqa: E402
from core.config import Settings  # noqa: E402
from core.hub import HubError  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    # Reserve private evidence before network work. Never replace an old receipt.
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        try:
            out = run.reconcile(Settings(), dry_run=True)
            document = json.dumps(asdict(out), ensure_ascii=False, indent=2, allow_nan=False)
        except Exception as exc:
            # Transport and validation messages may contain private response data.
            # Emit only the exception category; do not print the original traceback.
            failure = (
                exc.diagnostic if isinstance(exc, HubError) else {"category": type(exc).__name__}
            )
            receipt = {
                "dry_run": True,
                "errors": [type(exc).__name__],
                "incomplete": True,
                "failure": failure,
            }
            json.dump(receipt, stream)
            print(json.dumps(receipt))
            return 1
        stream.write(document + "\n")
    print(
        json.dumps(
            {
                "dry_run": True,
                "planned": len(out.planned),
                "reviews": len(out.review_items),
                "errors": len(out.errors),
                "output": str(args.output),
            }
        )
    )
    return 1 if out.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
