"""Unit tests for scripts/spotify_auth.py - arg parsing, authorize URL, callback server.

No browser, no live Spotify: the server test drives the local http.server with
httpx; token exchange uses a MockTransport.
"""

import importlib.util
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

spec = importlib.util.spec_from_file_location(
    "spotify_auth", Path(__file__).parent.parent / "scripts" / "spotify_auth.py"
)
auth = importlib.util.module_from_spec(spec)
spec.loader.exec_module(auth)

SCOPES = [
    "playlist-read-private",
    "playlist-read-collaborative",
    "playlist-modify-private",
    "playlist-modify-public",
    "user-library-read",
    "user-library-modify",
    "user-follow-read",
]


def test_parse_args_from_flags():
    args = auth.parse_args(["--client-id", "cid", "--client-secret", "csec"])
    assert (args.client_id, args.client_secret, args.port) == ("cid", "csec", 8080)


def test_parse_args_from_env(monkeypatch):
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "envid")
    monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "envsec")
    args = auth.parse_args([])
    assert (args.client_id, args.client_secret) == ("envid", "envsec")


def test_parse_args_requires_client_id(monkeypatch):
    monkeypatch.delenv("SPOTIFY_CLIENT_ID", raising=False)
    monkeypatch.delenv("SPOTIFY_CLIENT_SECRET", raising=False)
    with pytest.raises(SystemExit):
        auth.parse_args([])


def test_authorize_url_has_all_scopes_pkce_and_redirect():
    url = auth.build_authorize_url("cid", "state123", "challenge456", 8888)
    q = parse_qs(urlparse(url).query)
    assert q["client_id"] == ["cid"]
    assert q["response_type"] == ["code"]
    assert q["redirect_uri"] == ["http://127.0.0.1:8888/callback"]
    assert q["state"] == ["state123"]
    assert q["code_challenge"] == ["challenge456"]
    assert q["code_challenge_method"] == ["S256"]
    assert sorted(q["scope"][0].split()) == sorted(SCOPES)


def test_callback_server_binds_localhost_and_captures_code():
    server = auth.make_callback_server(port=0)  # ephemeral port: bind check only
    host, port = server.server_address[:2]
    assert host == "127.0.0.1"
    t = threading.Thread(target=server.handle_request)
    t.start()
    resp = httpx.get(f"http://127.0.0.1:{port}/callback", params={"code": "c0de", "state": "st"})
    t.join(timeout=5)
    server.server_close()
    assert resp.status_code == 200
    assert server.result == {"code": "c0de", "state": "st"}


def test_callback_server_captures_error():
    server = auth.make_callback_server(port=0)
    port = server.server_address[1]
    t = threading.Thread(target=server.handle_request)
    t.start()
    httpx.get(f"http://127.0.0.1:{port}/callback", params={"error": "access_denied"})
    t.join(timeout=5)
    server.server_close()
    assert server.result["error"] == "access_denied"


def test_exchange_code_posts_pkce_verifier_and_returns_tokens():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization", "")
        seen["body"] = parse_qs(request.read().decode())
        return httpx.Response(
            200, json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600}
        )

    http = httpx.Client(transport=httpx.MockTransport(handler))
    tokens = auth.exchange_code("cid", "csec", "c0de", "verif", 8888, http=http)
    assert tokens["refresh_token"] == "rt"
    assert seen["url"] == "https://accounts.spotify.com/api/token"
    assert seen["auth"].startswith("Basic ")
    assert seen["body"]["grant_type"] == ["authorization_code"]
    assert seen["body"]["code"] == ["c0de"]
    assert seen["body"]["code_verifier"] == ["verif"]
    assert seen["body"]["redirect_uri"] == ["http://127.0.0.1:8888/callback"]


def test_main_no_browser_emits_only_refresh_json(monkeypatch, capsys):
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "cid")
    monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "private-client-secret")
    monkeypatch.setattr(auth.sys, "argv", ["spotify_auth.py", "--no-browser"])
    monkeypatch.setattr(auth.secrets, "token_urlsafe", lambda _: "state")
    monkeypatch.setattr(
        auth,
        "make_callback_server",
        lambda _: SimpleNamespace(
            result={"code": "code", "state": "state"}, server_close=lambda: None
        ),
    )
    monkeypatch.setattr(
        auth, "exchange_code", lambda *args: {"refresh_token": "private-refresh-token"}
    )
    monkeypatch.setattr(auth.webbrowser, "open", lambda _: pytest.fail("opened local browser"))
    auth.main()
    out, err = capsys.readouterr()
    assert json.loads(out) == {"refresh_token": "private-refresh-token"}
    assert "private-client-secret" not in out + err
    assert "private-refresh-token" not in err
    assert "https://accounts.spotify.com/authorize?" in err
