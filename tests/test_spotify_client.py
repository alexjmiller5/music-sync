"""httpx.MockTransport, no network."""

import json

import httpx
import pytest

from core.spotify_client import MAX_429_RETRIES, MAX_429_WAIT, SpotifyAuthError, SpotifyClient


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
            return httpx.Response(
                200, json={"items": [{"added_at": "b"}], "next": None, "total": 2}
            )
        return httpx.Response(
            200,
            json={
                "items": [{"added_at": "a"}],
                "total": 2,
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


@pytest.mark.parametrize(
    "page",
    [
        {},
        {"items": None, "next": None},
        {"items": [], "next": ""},
        {"items": [], "next": 1},
        {"items": [{}]},
        {"items": [None], "next": None},
        {"items": [], "next": None, "total": 1},
        {"items": [], "next": None, "total": True},
        {"items": [], "next": None, "total": None},
        {"items": [{}], "next": None, "offset": 1},
        {"items": [{}], "total": 2, "next": "https://untrusted.invalid/v1/me/tracks"},
        {"items": [{}], "total": 2, "next": "http://api.spotify.com/v1/me/tracks"},
        {"items": [{}], "total": 2, "next": "https://api.spotify.com/v1/me/playlists"},
    ],
)
def test_incomplete_or_invalid_pages_never_become_unlikes(settings, mocker, page):
    calls = []

    def handler(req):
        if req.url.host == "accounts.spotify.com":
            return token_resp()
        calls.append(req.url)
        assert len(calls) == 1, "must reject the invalid page before following its URL"
        return httpx.Response(200, json={"total": 1, **page})

    with pytest.raises(ValueError, match="Spotify pagination"):
        make(handler, settings, mocker).get_liked("US")


def test_repeated_page_url_stops_before_refetch(settings, mocker):
    calls = []

    def handler(req):
        if req.url.host == "accounts.spotify.com":
            return token_resp()
        calls.append(req.url)
        assert len(calls) <= 2, "repeated cursor must not loop"
        return httpx.Response(
            200,
            json={
                "items": [{}],
                "next": "https://api.spotify.com/v1/me/tracks?offset=1",
                "total": 4,
            },
        )

    with pytest.raises(ValueError, match="Spotify pagination"):
        make(handler, settings, mocker).get_liked("US")
    assert len(calls) == 2


@pytest.mark.parametrize("last_total", [1, 3])
def test_changing_total_does_not_produce_complete_snapshot(settings, mocker, last_total):
    pages = iter(
        [
            {"items": [{}], "next": "https://api.spotify.com/v1/me/tracks?offset=1", "total": 2},
            {"items": [{}], "next": None, "total": last_total},
        ]
    )

    def handler(req):
        if req.url.host == "accounts.spotify.com":
            return token_resp()
        return httpx.Response(200, json=next(pages))

    with pytest.raises(ValueError, match="Spotify pagination"):
        make(handler, settings, mocker).get_liked("US")


@pytest.mark.parametrize("item", [{}, {"track": None}, {"track": {}}, {"track": {"id": ""}}])
def test_unidentified_liked_item_cannot_prove_a_complete_liked_collection(settings, mocker, item):
    def handler(req):
        if req.url.host == "accounts.spotify.com":
            return token_resp()
        return httpx.Response(200, json={"items": [item], "next": None, "total": 1})

    with pytest.raises(ValueError, match="Spotify pagination"):
        make(handler, settings, mocker).get_liked("US")


def test_followed_artists_reads_all_cursor_pages_without_mutations(settings, mocker):
    seen = []

    def handler(req):
        if req.url.host == "accounts.spotify.com":
            return token_resp()
        assert req.method == "GET" and req.url.path == "/v1/me/following"
        seen.append(dict(req.url.params))
        if req.url.params.get("after") == "artist-one":
            return httpx.Response(
                200,
                json={
                    "artists": {
                        "items": [{"id": "artist-two", "name": "Artist Two"}],
                        "next": None,
                        "total": 2,
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "artists": {
                    "items": [{"id": "artist-one", "name": "Artist One"}],
                    "total": 2,
                    "next": "https://api.spotify.com/v1/me/following?type=artist&limit=50&after=artist-one",
                }
            },
        )

    rows = make(handler, settings, mocker).get_followed_artists()
    assert [r["id"] for r in rows] == ["artist-one", "artist-two"]
    assert seen[0] == {"type": "artist", "limit": "50"}
    assert seen[1]["after"] == "artist-one"


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
    assert c.add_items("P", ["spotify:track:3"], position=4) == "s"
    assert c.remove_items("P", ["spotify:track:2"]) == "s"
    # Removal is by URI: every occurrence of a URI leaves the playlist.
    assert calls == [
        ("POST", "/v1/playlists/P/items", {"uris": ["spotify:track:1"]}),
        ("POST", "/v1/playlists/P/items", {"uris": ["spotify:track:3"], "position": 4}),
        ("DELETE", "/v1/playlists/P/items", {"items": [{"uri": "spotify:track:2"}]}),
    ]


def test_positioned_add_keeps_order_across_chunks(settings, mocker):
    calls = []

    def handler(req):
        if req.url.host == "accounts.spotify.com":
            return token_resp()
        calls.append(json.loads(req.content))
        return httpx.Response(201, json={"snapshot_id": "s"})

    uris = [f"spotify:track:{i}" for i in range(150)]
    make(handler, settings, mocker).add_items("P", uris, position=7)
    assert [c["position"] for c in calls] == [7, 107]
    assert [u for c in calls for u in c["uris"]] == uris


def test_create_rename_and_unlike(settings, mocker):
    calls = []

    def handler(req):
        if req.url.host == "accounts.spotify.com":
            return token_resp()
        body = json.loads(req.content) if req.content else None
        calls.append((req.method, req.url.path, dict(req.url.params), body))
        if req.method == "POST":
            return httpx.Response(201, json={"id": "NEW", "snapshot_id": "s0"})
        return httpx.Response(200)

    c = make(handler, settings, mocker)
    created = c.create_playlist("older", "smart", public=False)
    c.rename_playlist("P", "older (pre-sync)")
    c.unlike([f"spotify:track:{i}" for i in range(41)])
    assert created["id"] == "NEW"
    assert calls[0] == (
        "POST",
        "/v1/me/playlists",
        {},
        {"name": "older", "public": False, "description": "smart"},
    )
    assert calls[1] == ("PUT", "/v1/playlists/P", {}, {"name": "older (pre-sync)"})
    assert [(m, p) for m, p, _, _ in calls[2:]] == [("DELETE", "/v1/me/library")] * 2
    assert len(calls[2][2]["uris"].split(",")) == 40


def test_set_description_truncates_to_300(settings, mocker):
    calls = []

    def handler(req):
        if req.url.host == "accounts.spotify.com":
            return token_resp()
        calls.append((req.method, req.url.path, json.loads(req.content)))
        return httpx.Response(200)

    make(handler, settings, mocker).set_description("P", "x" * 400)
    assert calls == [("PUT", "/v1/playlists/P", {"description": "x" * 300})]


def test_unfollow_playlist_removes_it_from_the_library(settings, mocker):
    calls = []

    def handler(req):
        if req.url.host == "accounts.spotify.com":
            return token_resp()
        calls.append((req.method, req.url.path, req.url.params.get("uris")))
        return httpx.Response(200)

    make(handler, settings, mocker).unfollow_playlist("P")
    # The followers endpoint was removed in February 2026.
    assert calls == [("DELETE", "/v1/me/library", "spotify:playlist:P")]


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


def test_429_with_long_retry_after_fails_fast_without_sleeping(settings, mocker):
    calls = []

    def handler(req):
        if req.url.host == "accounts.spotify.com":
            return token_resp()
        calls.append(req)
        return httpx.Response(429, headers={"Retry-After": str(int(MAX_429_WAIT) + 1)})

    client = make(handler, settings, mocker)
    sleep = mocker.patch("core.spotify_client.time.sleep")
    with pytest.raises(httpx.HTTPStatusError) as exc:
        client.me()
    assert exc.value.response.status_code == 429
    assert len(calls) == 1
    sleep.assert_not_called()
