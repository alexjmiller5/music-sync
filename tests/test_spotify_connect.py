"""Connect Spotify: a person approves Music Sync with their own Spotify login."""

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from core import spotify_connect as sc
from core import workspaces

NOW = datetime(2026, 10, 2, tzinfo=timezone.utc)
REDIRECT = "https://ws--spotify-connect.modal.run/"


@pytest.fixture
def objects(monkeypatch):
    stored = {}
    for mod in (sc, workspaces):
        monkeypatch.setattr(mod.archive, "get", lambda settings, key: stored.get(key))
        monkeypatch.setattr(
            mod.archive, "put", lambda settings, key, value: stored.__setitem__(key, value)
        )
    return stored


def _token_http(captured):
    def handler(request):
        captured.append(request)
        return httpx.Response(
            200, json={"access_token": "at", "refresh_token": "friend-refresh", "scope": sc.SCOPES}
        )

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_invite_leads_to_spotify_with_this_app_and_state(settings, objects):
    workspaces.save(settings, "friend", notion_token="n")
    invite = sc.issue_invite(settings, "friend", NOW)
    url = urlsplit(sc.authorize_url(settings, invite, REDIRECT, NOW))
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert url.netloc == "accounts.spotify.com"
    assert q["client_id"] == settings.spotify_client_id
    assert q["redirect_uri"] == REDIRECT and q["state"] == invite and q["scope"] == sc.SCOPES
    assert q["response_type"] == "code"


def test_callback_stores_the_persons_refresh_token_on_their_workspace(settings, objects):
    workspaces.save(settings, "friend", notion_token="n")
    invite = sc.issue_invite(settings, "friend", NOW)
    captured = []
    wid = sc.complete(settings, invite, "auth-code", REDIRECT, NOW, _token_http(captured))
    assert wid == "friend"
    assert workspaces.settings_for(settings, "friend").spotify_refresh_token == "friend-refresh"
    body = parse_qs(captured[0].content.decode())
    assert body == {
        "grant_type": ["authorization_code"],
        "code": ["auth-code"],
        "redirect_uri": [REDIRECT],
    }
    assert captured[0].headers["authorization"].startswith("Basic ")


def test_an_invite_works_once(settings, objects):
    workspaces.save(settings, "friend", notion_token="n")
    invite = sc.issue_invite(settings, "friend", NOW)
    sc.complete(settings, invite, "c1", REDIRECT, NOW, _token_http([]))
    with pytest.raises(sc.InvalidInvite):
        sc.complete(settings, invite, "c2", REDIRECT, NOW, _token_http([]))
    with pytest.raises(sc.InvalidInvite):
        sc.authorize_url(settings, invite, REDIRECT, NOW)


def test_expired_and_unknown_invites_are_refused(settings, objects):
    workspaces.save(settings, "friend", notion_token="n")
    invite = sc.issue_invite(settings, "friend", NOW, ttl=timedelta(hours=1))
    with pytest.raises(sc.InvalidInvite):
        sc.authorize_url(settings, invite, REDIRECT, NOW + timedelta(hours=2))
    with pytest.raises(sc.InvalidInvite):
        sc.authorize_url(settings, "made-up", REDIRECT, NOW)


def test_invites_need_an_existing_workspace(settings, objects):
    with pytest.raises(workspaces.UnknownWorkspace):
        sc.issue_invite(settings, "ghost", NOW)


def test_scopes_match_the_operator_script():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "spotify_auth.py"
    spec = importlib.util.spec_from_file_location("spotify_auth", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.SCOPES == sc.SCOPES
