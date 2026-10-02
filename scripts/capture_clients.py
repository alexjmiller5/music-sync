"""Issue or revoke capture-access tokens for consumer apps (operator tool).

    uv run scripts/capture_clients.py issue "<device label>" [workspace]   # prints the enrollment link
    uv run scripts/capture_clients.py revoke <client_id>

Runs the capture_access operation on the deployed worker with the operator's Modal auth
(MODAL_TOKEN_ID / MODAL_TOKEN_SECRET or a modal profile). The link is the only
copy of the token: send it to the device owner, who opens it on that device.
"""

import sys

import modal

sys.path.insert(0, "src")
from core.capture_clients import enrollment_link  # noqa: E402

APP_NAME = "music-sync"


def main(argv: list[str]) -> int:
    worker = modal.Function.from_name(APP_NAME, "worker")

    def access(body: dict):
        # The capture-access web endpoint forwards to this same serialized worker;
        # a web endpoint itself cannot be called with .remote().
        result = worker.remote("capture_access", body)
        return (
            result
            if isinstance(result, dict)
            else {"ok": False, "detail": getattr(result, "body", result)}
        )

    match [a for a in argv if a]:
        case ["issue", label, *rest] if len(rest) <= 1:
            body = {"action": "issue", "label": label}
            if rest:
                body["workspace"] = rest[0]
            issued = access(body)
            if not isinstance(issued, dict) or not issued.get("ok"):
                print(f"issue failed: {issued}", file=sys.stderr)
                return 1
            enroll = modal.Function.from_name(APP_NAME, "capture_enroll").get_web_url()
            capture = modal.Function.from_name(APP_NAME, "capture_consumer").get_web_url()
            print(
                f"client_id: {issued['client_id']}  label: {label}  workspace: {rest[0] if rest else 'default'}"
            )
            print(enrollment_link(enroll, capture, issued["token"]))
        case ["revoke", client_id]:
            result = access({"action": "revoke", "client_id": client_id})
            print(result)
            return 0 if result.get("ok") else 1
        case _:
            print(__doc__, file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
