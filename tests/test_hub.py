import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone

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


def test_push_chunks_stamp_once_per_invocation_without_changing_rows(monkeypatch):
    import core.hub

    class Clock:
        value = datetime(2026, 9, 9, 12, 0, 0, 123456, tzinfo=timezone.utc)

        @classmethod
        def now(cls, tz):
            assert tz == timezone.utc
            value = cls.value
            cls.value += timedelta(seconds=1)
            return value

    monkeypatch.setattr(core.hub, "datetime", Clock, raising=False)
    bodies = []

    def handler(req):
        bodies.append(json.loads(req.content))
        return httpx.Response(
            200, json={"upserted": len(bodies[-1]["rows"]), "rejected": [], "hub_at": "t"}
        )

    supplied = "2026-09-08T10:11:12.987Z"
    rows = [{"id": str(i), "liked": 1} for i in range(501)] + [
        {"id": "x", "first_seen": None},
        {"id": "y", "liked": 0, "updated_at": supplied},
    ]
    before = deepcopy(rows)
    hub = make(handler)
    out = hub.push("songs", rows)
    assert out["upserted"] == 503 and len(bodies) == 3
    assert bodies[0]["columns"] == ["id", "liked", "updated_at"]
    stamp = "2026-09-09T12:00:00.123Z"
    assert bodies[0]["rows"][0] == {"id": "0", "liked": 1, "updated_at": stamp}
    for body in bodies:
        for row in body["rows"]:
            assert row["updated_at"] == (supplied if row["id"] == "y" else stamp)
            assert sorted(row) == body["columns"]
    assert bodies[-1]["rows"] == [{"id": "x", "first_seen": None, "updated_at": stamp}]
    assert rows == before
    hub.push("songs", [{"id": "z"}])
    assert bodies[-1]["rows"] == [{"id": "z", "updated_at": "2026-09-09T12:00:01.123Z"}]


def test_push_preserves_supplied_null_timestamp_for_hub_rejection():
    def handler(req):
        body = json.loads(req.content)
        assert body["rows"] == [{"id": "A", "updated_at": None}]
        return httpx.Response(
            200,
            json={
                "upserted": 0,
                "rejected": [{"id": "A", "col": "updated_at", "rule": "required"}],
            },
        )

    with pytest.raises(HubError, match="required"):
        make(handler).push("songs", [{"id": "A", "updated_at": None}])


def test_push_normalizes_spotify_dates_without_changing_json_or_omissions():
    import sqlite3

    rows = [
        {"id": "A", "liked_at": "2026-09-09T08:00:00.987654-04:00", "detail": {"created_row": 1}},
        {"id": "B", "added_at": "2026-09-09T12:00:00Z", "spotify_ids": ["dummy"]},
        {"id": "C", "liked_at": None},
    ]
    before = deepcopy(rows)
    received = []

    def handler(req):
        body = json.loads(req.content)
        received.extend(body["rows"])
        # The worker binds JSON.stringify(rows) and extracts each value in SQLite.
        with sqlite3.connect(":memory:") as db:
            stored = db.execute(
                "SELECT json_extract(value, '$.detail'), json_extract(value, '$.spotify_ids') "
                "FROM json_each(?)",
                (json.dumps(body["rows"]),),
            ).fetchone()
        if body["rows"][0]["id"] == "A":
            assert stored == ('{"created_row":1}', None)
        elif body["rows"][0]["id"] == "B":
            assert stored == (None, '["dummy"]')
        return httpx.Response(200, json={"upserted": len(body["rows"]), "rejected": []})

    assert make(handler).push("songs", rows)["upserted"] == 3
    assert [{k: v for k, v in row.items() if k != "updated_at"} for row in received] == [
        {"id": "A", "liked_at": "2026-09-09T12:00:00.987Z", "detail": {"created_row": 1}},
        {"id": "B", "added_at": "2026-09-09T12:00:00.000Z", "spotify_ids": ["dummy"]},
        {"id": "C", "liked_at": None},
    ]
    assert rows == before


@pytest.mark.parametrize("value", ["bad-date", "2026-09-09T12:00:00", 123])
def test_push_rejects_invalid_or_naive_spotify_dates_before_sending(value):
    bodies = []

    def handler(req):
        bodies.append(json.loads(req.content))
        return httpx.Response(200, json={"upserted": 1, "rejected": []})

    with pytest.raises(HubError, match="added_at"):
        make(handler).push("playlist_songs", [{"id": "A", "added_at": value}])
    assert bodies == []


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


def test_derive_forwards_optional_column_in_each_request():
    bodies = []

    def handler(req):
        bodies.append(json.loads(req.content))
        return httpx.Response(200, json={"derived": len(bodies[-1]["ids"]), "failed": []})

    out = make(handler).derive("songs", [str(i) for i in range(51)], col="mb_tags")
    assert [len(b["ids"]) for b in bodies] == [50, 1]
    assert all(b["col"] == "mb_tags" for b in bodies)
    assert out == {"derived": 51, "failed": []}


def test_http_error_is_huberror():
    def handler(req):
        return httpx.Response(500, text="boom")

    with pytest.raises(HubError, match="500"):
        make(handler).pull("songs", ["id"])
