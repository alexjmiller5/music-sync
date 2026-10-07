"""Read followed artists with Music Sync's existing configuration; no imports or writes.

uv run scripts/followed_artists.py --output /private/state/followed-artists.json
Personal source evidence belongs outside source control. Follow dates are unknown.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.config import Settings  # noqa: E402
from core.spotify_client import SpotifyClient  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        started = datetime.now(timezone.utc).isoformat()
        try:
            source = SpotifyClient(Settings())
            account = source.me()["id"]
            if not isinstance(account, str) or not account:
                raise ValueError("missing source account identity")
            receipt = {
                "complete": True,
                "observed_at": started,
                "account_id": account,
                "artists": source.get_followed_artists(),
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "follow_dates_known": False,
                "atomic": False,
            }
            document = json.dumps(receipt, ensure_ascii=False, indent=2, allow_nan=False)
        except Exception as exc:
            receipt = {"complete": False, "observed_at": started, "error": type(exc).__name__}
            if isinstance(exc, httpx.HTTPStatusError):
                receipt["http_status"] = exc.response.status_code
            json.dump(receipt, stream)
            print(json.dumps(receipt))
            return 1
        stream.write(document + "\n")
    print(json.dumps({"complete": True, "artists": len(receipt["artists"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
