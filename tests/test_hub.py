import json

import httpx
import pytest

from core.hub import Hub, HubError


def make(handler):
    return Hub(
        "https://hub.test/", "tok", http=httpx.Client(transport=httpx.MockTransport(handler))
    )


def test_pull_posts_table_columns_since_with_bearer_and_user_agent():
    seen = {}

    def handler(req):
        seen["url"], seen["body"], seen["h"] = str(req.url), json.loads(req.content), req.headers
        return httpx.Response(200, json={"rows": [{"id": "A"}]})

    assert make(handler).pull("songs", ["id"]) == [{"id": "A"}]
    assert seen["url"] == "https://hub.test/v1/rows/pull"
    assert seen["body"] == {"table": "songs", "columns": ["id"], "since": ""}
    assert seen["h"]["Authorization"] == "Bearer tok"
    assert "music-sync" in seen["h"]["User-Agent"]


def test_push_chunks_with_exact_key_groups():
    bodies = []

    def handler(req):
        bodies.append(json.loads(req.content))
        return httpx.Response(
            200, json={"upserted": len(bodies[-1]["rows"]), "rejected": [], "hub_at": "t"}
        )

    rows = [{"id": str(i), "liked": 1} for i in range(501)] + [{"id": "x", "first_seen": "s"}]
    out = make(handler).push("songs", rows)
    assert out["upserted"] == 502 and len(bodies) == 3
    assert bodies[0]["columns"] == ["id", "liked"]
    assert bodies[0]["rows"][0] == {"id": "0", "liked": 1}


def test_push_raises_on_rejection():
    def handler(req):
        return httpx.Response(
            200, json={"upserted": 0, "rejected": [{"id": "A", "rule": "pattern"}], "hub_at": "t"}
        )

    with pytest.raises(HubError, match="pattern"):
        make(handler).push("songs", [{"id": "A"}])


def test_derive_chunks_of_50():
    sizes = []

    def handler(req):
        sizes.append(len(json.loads(req.content)["ids"]))
        return httpx.Response(200, json={"derived": sizes[-1], "failed": []})

    out = make(handler).derive("songs", [str(i) for i in range(120)])
    assert sizes == [50, 50, 20] and out == {"derived": 120, "failed": []}


def test_http_error_is_huberror():
    def handler(req):
        return httpx.Response(500, text="boom")

    with pytest.raises(HubError, match="500"):
        make(handler).pull("songs", ["id"])
