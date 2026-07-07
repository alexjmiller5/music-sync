# /// script
# requires-python = ">=3.13"
# dependencies = ["httpx>=0.28"]
# ///
"""One-time Spotify auth: mint a refresh token via authorization-code + PKCE.

Run AFTER creating the Spotify developer app (https://developer.spotify.com/dashboard)
with redirect URI http://127.0.0.1:8888/callback:

    SPOTIFY_CLIENT_ID=... SPOTIFY_CLIENT_SECRET=... uv run scripts/spotify_auth.py

Opens the browser, captures the callback on 127.0.0.1:8888, exchanges the code,
then PRINTS the refresh token plus the exact `op item create` / `op item edit`
commands for Alex to paste into HIS shell (Claude is denied `op item edit`).
Nothing is ever written to disk.
"""

import argparse
import base64
import hashlib
import http.server
import os
import secrets
import sys
import webbrowser
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
SCOPES = (
    "playlist-read-private playlist-read-collaborative "
    "playlist-modify-private playlist-modify-public "
    "user-follow-read user-library-read"
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-id", default=os.environ.get("SPOTIFY_CLIENT_ID"))
    parser.add_argument("--client-secret", default=os.environ.get("SPOTIFY_CLIENT_SECRET"))
    parser.add_argument("--port", type=int, default=8888)
    args = parser.parse_args(argv)
    if not args.client_id or not args.client_secret:
        parser.error(
            "missing --client-id/--client-secret (or SPOTIFY_CLIENT_ID/SPOTIFY_CLIENT_SECRET env)"
        )
    return args


def _redirect_uri(port: int) -> str:
    return f"http://127.0.0.1:{port}/callback"


def build_authorize_url(client_id: str, state: str, code_challenge: str, port: int) -> str:
    return (
        AUTHORIZE_URL
        + "?"
        + urlencode(
            {
                "client_id": client_id,
                "response_type": "code",
                "redirect_uri": _redirect_uri(port),
                "scope": SCOPES,
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
    )


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (stdlib API name)
        parsed = urlparse(self.path)
        if parsed.path != "/callback":
            self.send_error(404)
            return
        self.server.result = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Spotify auth captured - return to the terminal.")

    def log_message(self, *args):  # keep stdout clean for the op commands
        pass


def make_callback_server(port: int) -> http.server.HTTPServer:
    server = http.server.HTTPServer(("127.0.0.1", port), _CallbackHandler)
    server.result = None
    return server


def exchange_code(
    client_id: str,
    client_secret: str,
    code: str,
    code_verifier: str,
    port: int,
    http: httpx.Client | None = None,
) -> dict:
    client = http or httpx.Client(timeout=30)
    resp = client.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _redirect_uri(port),
            "code_verifier": code_verifier,
        },
        auth=(client_id, client_secret),
    )
    resp.raise_for_status()
    return resp.json()


def print_op_commands(client_id: str, client_secret: str, refresh_token: str) -> None:
    print("\nRefresh token minted. Save it to 1Password by pasting ONE of these")
    print("into your own terminal (Claude is denied op item create/edit):\n")
    print("# if the 'Spotify API' item does NOT exist yet in OpenClaw:")
    print(
        f"op item create --vault OpenClaw --category 'API Credential' --title 'Spotify API' "
        f"client_id={client_id} client_secret={client_secret} refresh_token={refresh_token}"
    )
    print("\n# if it already exists:")
    print(f"op item edit 'Spotify API' --vault OpenClaw refresh_token={refresh_token}")
    print("\nNothing was written to disk.")


def main() -> None:
    args = parse_args()
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    state = secrets.token_urlsafe(16)

    server = make_callback_server(args.port)
    url = build_authorize_url(args.client_id, state, challenge, args.port)
    print(f"Opening browser for Spotify consent (listening on {_redirect_uri(args.port)})...")
    print(f"If it does not open, visit:\n{url}")
    webbrowser.open(url)
    while server.result is None:
        server.handle_request()
    server.server_close()

    if "error" in server.result:
        sys.exit(f"Spotify returned error: {server.result['error']}")
    if server.result.get("state") != state:
        sys.exit("State mismatch - possible CSRF, aborting.")

    tokens = exchange_code(
        args.client_id, args.client_secret, server.result["code"], verifier, args.port
    )
    print_op_commands(args.client_id, args.client_secret, tokens["refresh_token"])


if __name__ == "__main__":
    main()
