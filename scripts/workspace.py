"""Manage Music Sync workspaces (operator tool).

    uv run scripts/workspace.py list
    uv run scripts/workspace.py show <id>
    uv run scripts/workspace.py set <id>            # KEY=VALUE lines on stdin
    uv run scripts/workspace.py connect-link <id>   # prints a one-time Connect Spotify link

`default` is the operator's own workspace, configured by env. Any other one is
a record in the app's state overriding the per-user fields: SPOTIFY_MARKET,
LIFE_HUB_URL, LIFE_HUB_TOKEN, NOTION_TOKEN, NOTION_TASKS_DATA_SOURCE_ID,
NOTION_PROJECT_PAGE_ID, INBOX_CAP, UNDO_DAYS, RECONCILE_ENABLED (its own
switch; the app-wide RECONCILE_ENABLED must also be on). The Spotify refresh
token comes from the person approving Music Sync through `connect-link` (add
their Spotify email to the app's user allowlist in the Spotify dashboard
first). Values go on stdin, never as arguments. A new person = `set` their
hub/Notion, send the connect link, then `just clients issue "<device>" <id>`.

Runs on the deployed worker with the operator's Modal auth.
"""

import json
import sys

import modal

APP_NAME = "music-sync"


def main(argv: list[str]) -> int:
    worker = modal.Function.from_name(APP_NAME, "worker")

    def admin(**body):
        return worker.remote("workspace_admin", body)

    match [a for a in argv if a]:
        case ["list"]:
            print("\n".join(admin(action="list")["workspaces"]))
        case ["show", wid]:
            print(json.dumps(admin(action="show", workspace=wid), indent=2, sort_keys=True))
        case ["set", wid]:
            fields = {}
            for line in sys.stdin.read().splitlines():
                key, sep, value = line.partition("=")
                if sep and key.strip():
                    fields[key.strip().lower()] = value.strip()
            summary = admin(action="save", workspace=wid, fields=fields)
            print(f"set {sorted(fields)} on {wid}; now set: {summary['set']}")
        case ["connect-link", wid]:
            invite = admin(action="connect_link", workspace=wid)["invite"]
            url = modal.Function.from_name(APP_NAME, "spotify_connect_endpoint").get_web_url()
            print(f"{url.rstrip('/')}/?invite={invite}")
        case _:
            print(__doc__, file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
