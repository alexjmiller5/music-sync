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


def test_catalog_gets_real_shape_with_auth():
    body = {
        "tables": [],
        "properties": [{"tbl": "songs", "col": "title", "derived_by": None}],
        "rules": [],
    }

    def handler(req):
        assert req.method == "GET" and req.url.path == "/v1/catalog"
        assert req.headers["Authorization"] == "Bearer tok"
        return httpx.Response(200, json=body)

    assert make(handler).catalog() == body


@pytest.mark.parametrize("status", [401, 503])
def test_catalog_http_errors(status):
    with pytest.raises(HubError, match=str(status)):
        make(lambda req: httpx.Response(status, text="unavailable")).catalog()


def test_pull_posts_table_columns_since_with_bearer_and_user_agent():
    seen = {}

    def handler(req):
        seen["url"], seen["body"], seen["h"] = str(req.url), json.loads(req.content), req.headers
        return httpx.Response(200, json={"next_cursor": None, "rows": [{"id": "A"}]})

    assert make(handler).pull("songs", ["id"]) == [{"id": "A"}]
    assert seen["url"] == "https://hub.test/v1/rows/pull"
    assert seen["body"] == {"table": "songs", "columns": ["id"], "since": "", "limit": 200}
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


def test_scan_retries_transient_hub_failures_but_not_client_errors(mocker):
    sleep = mocker.patch("core.hub.time.sleep")

    def worker_hub(handler):
        transport = httpx.Client(transport=httpx.MockTransport(handler))
        return Hub("https://hub.test", "t", transport, read_retries=(2, 5))

    replies = [
        httpx.Response(500, text='{"error":"D1_ERROR: Network connection lost."}'),
        httpx.Response(200, json={"rows": [{"id": "a"}], "next_cursor": None}),
    ]
    assert worker_hub(lambda r: replies.pop(0)).scan("songs", ["id"]) == [{"id": "a"}]
    assert sleep.call_count == 1
    with pytest.raises(HubError):
        worker_hub(lambda r: httpx.Response(403, text="scope")).scan("songs", ["id"])
    with pytest.raises(HubError):
        worker_hub(lambda r: httpx.Response(503, text="x")).scan("songs", ["id"])
    assert sleep.call_count == 1 + 2


def test_worker_clients_opt_into_read_retries():
    from core.hub import with_read_retries

    assert with_read_retries(Hub("https://hub.test", "t")).read_retries
    assert Hub("https://hub.test", "t").read_retries == ()


def test_insert_stamps_the_writer_clock_the_hub_requires():
    sent = []

    def handler(request):
        body = json.loads(request.content)
        sent.append(body)
        return httpx.Response(
            200, json={"inserted": [body["rows"][0]["id"]], "existing": [], "rejected": []}
        )

    make(handler).insert("tasks", {"id": "t1", "title": "x"})
    row = sent[0]["rows"][0]
    stamp = datetime.strptime(row["updated_at"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(
        tzinfo=timezone.utc
    )
    assert len(row["updated_at"]) == 24 and abs(datetime.now(timezone.utc) - stamp) < timedelta(
        minutes=1
    )
    assert sent[0]["columns"] == ["id", "title", "updated_at"]


def test_retained_files_go_through_the_file_api_with_their_type():
    seen = []

    def handler(req):
        seen.append((req.method, req.url.path, req.headers.get("Content-Type"), req.content))
        assert req.headers["Authorization"] == "Bearer tok"
        if req.method == "GET":
            return httpx.Response(200 if "a.jpg" in req.url.path else 404, content=b"img")
        return httpx.Response(200, json={"ok": True})

    hub = make(handler)
    hub.put_file("raw/spotify-covers/P/a.jpg", b"img", "image/jpeg")
    assert hub.get_file("raw/spotify-covers/P/a.jpg") == b"img"
    assert hub.get_file("raw/spotify-covers/P/gone.jpg") is None
    assert seen[0] == ("PUT", "/v1/files/raw/spotify-covers/P/a.jpg", "image/jpeg", b"img")
    with pytest.raises(HubError, match="403"):
        make(lambda req: httpx.Response(403, text="insufficient scope")).get_file("raw/x")


def test_post_failure_diagnostic_names_the_operation_without_row_values():
    client = make(lambda req: httpx.Response(500, text="private response"))
    with pytest.raises(HubError) as failure:
        client.insert_rows("records", [{"id": "private row"}])
    assert failure.value.diagnostic == {"category": "http", "status": 500}
    assert failure.value.route == "/v1/rows/insert"


@pytest.mark.parametrize("method,route", [("catalog", "/v1/catalog"), ("get_file", "/v1/files")])
def test_read_failures_name_only_the_operation(method, route):
    client = make(lambda req: httpx.Response(500, text="private response"))
    with pytest.raises(HubError) as failure:
        getattr(client, method)(*(["private-key"] if method == "get_file" else []))
    assert failure.value.route == route


def test_bounded_query_capability_and_pages_use_authenticated_read_protocol():
    bodies = []
    predicate = {"column": "id", "op": "in", "value": ["a", "b"]}

    def handler(req):
        assert req.headers["Authorization"] == "Bearer tok"
        if req.url.path == "/v1/session":
            return httpx.Response(200, json={"capabilities": {"row_query": "bounded-v1"}})
        assert req.url.path == "/v1/rows/query"
        body = json.loads(req.content)
        bodies.append(body)
        return httpx.Response(
            200,
            json={
                "rows": [{"id": "a" if len(bodies) == 1 else "b"}],
                "next_cursor": "page2" if len(bodies) == 1 else None,
            },
        )

    hub = make(handler)
    assert hub.supports_queries()
    assert hub.query("songs", ["id"], predicate) == [{"id": "a"}, {"id": "b"}]
    assert bodies[0] == {
        "table": "songs",
        "columns": ["id"],
        "filter": predicate,
        "order": [{"column": "id", "direction": "asc"}],
        "limit": 200,
    }
    assert bodies[1] == {**bodies[0], "cursor": "page2"}


@pytest.mark.parametrize("capabilities", [{}, {"row_query": "future-v2"}])
def test_older_hub_does_not_advertise_supported_query(capabilities):
    assert not make(
        lambda req: httpx.Response(200, json={"capabilities": capabilities})
    ).supports_queries()


@pytest.mark.parametrize(
    "page",
    [
        {"rows": [], "next_cursor": "same"},
        {"rows": [{"id": "a"}], "next_cursor": "same"},
        {"rows": [{"id": "a"}]},
        {"rows": "bad", "next_cursor": None},
        {"rows": [{"id": "a"}, {"id": "a"}], "next_cursor": None},
    ],
)
def test_bounded_query_rejects_incomplete_or_repeated_pages(page):
    calls = []

    def handler(req):
        calls.append(req)
        assert len(calls) <= 3
        return httpx.Response(200, json=page)

    with pytest.raises(HubError):
        make(handler).query("songs", ["id"])


def test_targeted_pull_batches_exhaust_each_record_without_broad_evidence_reads():
    calls = []

    def handler(req):
        body = json.loads(req.content)
        assert req.url.path == "/v1/rows/pull" and "batch" in body
        calls.append(body["batch"])
        pages = []
        for item in body["batch"]:
            assert item["since"] == ""
            assert item["where"]["to_kind"] == "songs"
            ref = item["where"]["to_ref"]
            more = ref == "a" and not item.get("after")
            pages.append(
                {
                    "rows": [{"id": ref + ("2" if item.get("after") else "1")}],
                    "next_cursor": ref + "1" if more else None,
                }
            )
        return httpx.Response(200, json={"batch": pages})

    assert make(handler).pull_many(
        "provenance", ["id"], "to_ref", ["a", "b"], {"to_kind": "songs"}
    ) == [{"id": "a1"}, {"id": "b1"}, {"id": "a2"}]
    assert len(calls) == 2 and [len(c) for c in calls] == [2, 1]
    assert calls[1][0]["after"] == "a1"


@pytest.mark.parametrize(
    "reply", [{}, {"batch": []}, {"batch": [{"rows": [], "next_cursor": "bad"}]}]
)
def test_targeted_pull_rejects_incomplete_batch_receipts(reply):
    with pytest.raises(HubError):
        make(lambda req: httpx.Response(200, json=reply)).pull_many(
            "provenance", ["id"], "to_ref", ["a"], {"to_kind": "songs"}
        )


@pytest.mark.parametrize("mode", ["query", "slice"])
def test_distinct_rows_cannot_hide_a_repeated_page_cursor(mode):
    calls = []

    def handler(req):
        calls.append(req)
        assert len(calls) < 4
        page = {"rows": [{"id": str(len(calls))}], "next_cursor": "repeated"}
        return httpx.Response(200, json=page if mode == "query" else {"batch": [page]})

    h = make(handler)
    with pytest.raises(HubError, match="cursor"):
        if mode == "query":
            h.query("songs", ["id"])
        else:
            h.pull_many("provenance", ["id"], "to_ref", ["a"], {"to_kind": "songs"})
