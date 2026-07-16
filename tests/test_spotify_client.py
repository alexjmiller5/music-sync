"""Unit tests for the Spotify Web API client — httpx.MockTransport, no network."""

import httpx
import pytest

from core.config import Settings
from core.models import FollowedArtist
from core.spotify_client import SpotifyClient, merge_followed_artists

SETTINGS = Settings(
    notion_token="nt",
    notion_parent_page_id="pp",
    spotify_client_id="cid",
    spotify_client_secret="csec",
    spotify_refresh_token="rtok",
)


def make_client(handler, mocker):
    mocker.patch("core.spotify_client.time.sleep")
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return SpotifyClient(SETTINGS, http=http)


def token_response(token="tok1"):
    return httpx.Response(200, json={"access_token": token, "expires_in": 3600})


def artist_item(spotify_id, name, genres=(), followers=0):
    return {
        "id": spotify_id,
        "name": name,
        "genres": list(genres),
        "followers": {"total": followers},
    }


def test_missing_creds_raises():
    with pytest.raises(RuntimeError, match="Spotify credentials"):
        SpotifyClient(Settings(notion_token="nt", notion_parent_page_id="pp"))


def test_token_refresh_uses_basic_auth_and_refresh_grant(mocker):
    seen = {}

    def handler(request):
        if request.url.host == "accounts.spotify.com":
            seen["auth"] = request.headers.get("Authorization", "")
            seen["body"] = request.content.decode()
            return token_response()
        seen["bearer"] = request.headers["Authorization"]
        return httpx.Response(200, json={"artists": {"items": [], "cursors": {"after": None}}})

    client = make_client(handler, mocker)
    client.get_followed_artists()
    assert seen["auth"].startswith("Basic ")
    assert "grant_type=refresh_token" in seen["body"]
    assert "refresh_token=rtok" in seen["body"]
    assert seen["bearer"] == "Bearer tok1"


def test_get_followed_artists_paginates_with_after_cursor(mocker):
    afters = []

    def handler(request):
        if request.url.host == "accounts.spotify.com":
            return token_response()
        afters.append(request.url.params.get("after"))
        if request.url.params.get("after") is None:
            return httpx.Response(
                200,
                json={
                    "artists": {
                        "items": [artist_item("a1", "GoldLink", ["rap"], 10)],
                        "cursors": {"after": "a1"},
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "artists": {
                    "items": [artist_item("a2", "The Strokes", ["rock"], 20)],
                    "cursors": {"after": None},
                }
            },
        )

    client = make_client(handler, mocker)
    artists = client.get_followed_artists()
    assert afters == [None, "a1"]
    assert artists == [
        FollowedArtist(name="GoldLink", spotify_id="a1", genres=["rap"], followers=10),
        FollowedArtist(name="The Strokes", spotify_id="a2", genres=["rock"], followers=20),
    ]


def test_401_refreshes_token_and_retries_once(mocker):
    tokens = iter(["stale", "fresh"])
    api_calls = []

    def handler(request):
        if request.url.host == "accounts.spotify.com":
            return token_response(next(tokens))
        api_calls.append(request.headers["Authorization"])
        if request.headers["Authorization"] == "Bearer stale":
            return httpx.Response(401, json={"error": {"status": 401}})
        return httpx.Response(200, json={"artists": {"items": [], "cursors": {"after": None}}})

    client = make_client(handler, mocker)
    assert client.get_followed_artists() == []
    assert api_calls == ["Bearer stale", "Bearer fresh"]


def test_second_401_after_refresh_raises(mocker):
    def handler(request):
        if request.url.host == "accounts.spotify.com":
            return token_response()
        return httpx.Response(401, json={})

    client = make_client(handler, mocker)
    with pytest.raises(httpx.HTTPStatusError):
        client.get_followed_artists()


def test_429_sleeps_retry_after_then_retries(mocker):
    calls = []

    def handler(request):
        if request.url.host == "accounts.spotify.com":
            return token_response()
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "3"}, json={})
        return httpx.Response(200, json={"artists": {"items": [], "cursors": {"after": None}}})

    sleep = mocker.patch("core.spotify_client.time.sleep")
    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = SpotifyClient(SETTINGS, http=http)
    assert client.get_followed_artists() == []
    assert len(calls) == 2
    assert any(c.args == (3.0,) for c in sleep.call_args_list)


def test_get_playlists_follows_next_url(mocker):
    def handler(request):
        if request.url.host == "accounts.spotify.com":
            return token_response()
        if request.url.path == "/v1/me/playlists" and "offset" not in request.url.params:
            return httpx.Response(
                200,
                json={
                    "items": [{"id": "p1"}],
                    "next": "https://api.spotify.com/v1/me/playlists?offset=50&limit=50",
                },
            )
        return httpx.Response(200, json={"items": [{"id": "p2"}], "next": None})

    client = make_client(handler, mocker)
    assert [p["id"] for p in client.get_playlists()] == ["p1", "p2"]


def test_get_playlist_tracks_follows_next_url(mocker):
    def handler(request):
        if request.url.host == "accounts.spotify.com":
            return token_response()
        assert request.url.path == "/v1/playlists/p1/tracks"
        if "offset" not in request.url.params:
            return httpx.Response(
                200,
                json={
                    "items": [{"track": {"id": "t1"}}],
                    "next": "https://api.spotify.com/v1/playlists/p1/tracks?offset=100",
                },
            )
        return httpx.Response(200, json={"items": [{"track": {"id": "t2"}}], "next": None})

    client = make_client(handler, mocker)
    tracks = client.get_playlist_tracks("p1")
    assert [t["track"]["id"] for t in tracks] == ["t1", "t2"]


# --- merge_followed_artists -----------------------------------------------------


def test_merge_live_enriches_export_by_spotify_id():
    export = [FollowedArtist(name="GoldLink", spotify_id="a1")]
    live = [FollowedArtist(name="GoldLink", spotify_id="a1", genres=["rap"], followers=10)]
    merged = merge_followed_artists(export, live)
    assert merged == live


def test_merge_keeps_export_only_and_adds_live_only():
    export = [FollowedArtist(name="Old Fave", spotify_id="a1")]
    live = [FollowedArtist(name="New Fave", spotify_id="a2", followers=5)]
    merged = merge_followed_artists(export, live)
    assert {a.spotify_id for a in merged} == {"a1", "a2"}
