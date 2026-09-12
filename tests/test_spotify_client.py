"""httpx.MockTransport, no network."""

import json

import httpx
import pytest

from core.spotify_client import MAX_429_RETRIES, SpotifyAuthError, SpotifyClient


def token_resp(tok="tok1"):
    return httpx.Response(200, json={"access_token": tok, "expires_in": 3600})


def make(handler, settings, mocker):
    mocker.patch("core.spotify_client.time.sleep")
    return SpotifyClient(settings, http=httpx.Client(transport=httpx.MockTransport(handler)))


def test_invalid_grant_raises_auth_error(settings, mocker):
    def handler(req):
        return httpx.Response(
            400, json={"error": "invalid_grant", "error_description": "Refresh token revoked"}
        )

    with pytest.raises(SpotifyAuthError, match="invalid_grant"):
        make(handler, settings, mocker).me()


def test_playlist_items_uses_items_path_market_and_fields(settings, mocker):
    seen = []

    def handler(req):
        if req.url.host == "accounts.spotify.com":
            return token_resp()
        seen.append(req.url)
        if req.url.params.get("offset") == "50":
            return httpx.Response(200, json={"items": [{"added_at": "b"}], "next": None})
        return httpx.Response(
            200,
            json={
                "items": [{"added_at": "a"}],
                "next": "https://api.spotify.com/v1/playlists/P/items?offset=50&limit=50",
            },
        )

    items = make(handler, settings, mocker).get_playlist_items("P", "US")
    assert [i["added_at"] for i in items] == ["a", "b"]
    assert seen[0].path == "/v1/playlists/P/items"
    assert seen[0].params["market"] == "US" and seen[0].params["limit"] == "50"
    assert "external_ids" in seen[0].params["fields"]
    assert "duration_ms" in seen[0].params["fields"]
    assert "linked_from(id)" in seen[0].params["fields"]


@pytest.mark.parametrize("count", [0, 40, 41, 81])
def test_like_chunks_library_saves_at_40_and_preserves_order(settings, mocker, count):
    calls = []
    uris = [f"spotify:track:{i}" for i in range(count)]

    def handler(req):
        if req.url.host == "accounts.spotify.com":
            return token_resp()
        batch = req.url.params.get("uris").split(",")
        if len(batch) > 40:
            return httpx.Response(400, json={"error": "too many uris"})
        calls.append((req.method, req.url.path, batch))
        return httpx.Response(200)

    make(handler, settings, mocker).like(uris)

    assert all(method == "PUT" and path == "/v1/me/library" for method, path, _ in calls)
    assert [uri for _, _, batch in calls for uri in batch] == uris


def test_add_and_remove_items_json_bodies(settings, mocker):
    calls = []

    def handler(req):
        if req.url.host == "accounts.spotify.com":
            return token_resp()
        calls.append((req.method, req.url.path, json.loads(req.content)))
        return httpx.Response(200, json={"snapshot_id": "s"})

    c = make(handler, settings, mocker)
    c.add_items("P", ["spotify:track:1"])
    c.remove_items("P", ["spotify:track:2"])
    assert calls == [
        ("POST", "/v1/playlists/P/items", {"uris": ["spotify:track:1"]}),
        ("DELETE", "/v1/playlists/P/items", {"uris": ["spotify:track:2"]}),
    ]


def test_set_description_truncates_to_300(settings, mocker):
    calls = []

    def handler(req):
        if req.url.host == "accounts.spotify.com":
            return token_resp()
        calls.append((req.method, req.url.path, json.loads(req.content)))
        return httpx.Response(200)

    make(handler, settings, mocker).set_description("P", "x" * 400)
    assert calls == [("PUT", "/v1/playlists/P", {"description": "x" * 300})]


def test_unfollow_playlist_deletes_followers(settings, mocker):
    calls = []

    def handler(req):
        if req.url.host == "accounts.spotify.com":
            return token_resp()
        calls.append((req.method, req.url.path))
        return httpx.Response(200)

    make(handler, settings, mocker).unfollow_playlist("P")
    assert calls == [("DELETE", "/v1/playlists/P/followers")]


def test_search_isrc_and_track_limit_10(settings, mocker):
    seen = []

    def handler(req):
        if req.url.host == "accounts.spotify.com":
            return token_resp()
        seen.append(dict(req.url.params))
        return httpx.Response(200, json={"tracks": {"items": [{"id": "t"}]}})

    c = make(handler, settings, mocker)
    assert c.search_isrc("GBBTV1101287", "US") == [{"id": "t"}]
    assert c.search_track("Take My Hand", "Matt Berry", "US") == [{"id": "t"}]
    assert seen[0] == {"q": "isrc:GBBTV1101287", "type": "track", "limit": "10", "market": "US"}
    assert seen[1]["q"] == "track:Take My Hand artist:Matt Berry"


def test_429_then_retry_and_401_refresh(settings, mocker):
    state = {"n": 0, "tokens": 0}

    def handler(req):
        if req.url.host == "accounts.spotify.com":
            state["tokens"] += 1
            return token_resp(f"tok{state['tokens']}")
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        if state["n"] == 2:
            return httpx.Response(401)
        return httpx.Response(200, json={"id": "me"})

    assert make(handler, settings, mocker).me() == {"id": "me"}
    assert state["tokens"] == 2


def test_429_retry_limit(settings, mocker):
    calls = []

    def handler(req):
        if req.url.host == "accounts.spotify.com":
            return token_resp()
        calls.append(req)
        return httpx.Response(429, headers={"Retry-After": "0"})

    with pytest.raises(httpx.HTTPStatusError):
        make(handler, settings, mocker).me()
    assert len(calls) == MAX_429_RETRIES + 1
