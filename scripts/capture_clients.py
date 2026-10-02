"""Issue or revoke capture-access tokens for consumer apps (operator tool).

    uv run scripts/capture_clients.py issue "<device label>"   # prints the enrollment link
    uv run scripts/capture_clients.py revoke <client_id>

Calls the deployed capture_access function with the operator's Modal auth
(MODAL_TOKEN_ID / MODAL_TOKEN_SECRET or a modal profile). The link is the only
copy of the token: send it to the device owner, who opens it on that device.
"""

import sys

import modal

sys.path.insert(0, "src")
from core.capture_clients import enrollment_link  # noqa: E402

APP_NAME = "music-sync"


def main(argv: list[str]) -> int:
    access = modal.Function.from_name(APP_NAME, "capture_access")
    match [a for a in argv if a]:
        case ["issue", label]:
            issued = access.remote({"action": "issue", "label": label})
            if not isinstance(issued, dict) or not issued.get("ok"):
                print(f"issue failed: {issued}", file=sys.stderr)
                return 1
            enroll = modal.Function.from_name(APP_NAME, "capture_enroll").get_web_url()
            capture = modal.Function.from_name(APP_NAME, "capture_consumer").get_web_url()
            print(f"client_id: {issued['client_id']}  label: {label}")
            print(enrollment_link(enroll, capture, issued["token"]))
        case ["revoke", client_id]:
            result = access.remote({"action": "revoke", "client_id": client_id})
            print(result if isinstance(result, dict) else "revoke failed")
        case _:
            print(__doc__, file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
