import json

import httpx

from core import flags


def test_no_flags_no_task(settings):
    assert flags.file(settings, httpx.Client(), [], [], "2026-09-08") is None


def test_creates_task_when_none_open(settings):
    seen = []

    def handler(req):
        seen.append((req.method, req.url.path, json.loads(req.content) if req.content else None))
        if req.url.path.endswith("/query"):
            return httpx.Response(200, json={"results": []})
        return httpx.Response(200, json={"id": "new-page"})

    pid = flags.file(
        settings, httpx.Client(transport=httpx.MockTransport(handler)), ["a"], ["b"], "2026-09-08"
    )
    assert pid == "new-page"
    method, path, body = seen[-1]
    assert (method, path) == ("POST", "/v1/pages")
    props = body["properties"]
    assert props["Name"]["title"][0]["text"]["content"] == "Music Sync flags"
    assert props["Priority"]["select"]["name"] == "High" and props["Tags"]["multi_select"] == [
        {"name": "Chore"}
    ]
    assert props["Due Date"]["date"]["start"] == "2026-09-08"
    assert props["Project"]["relation"] == [{"id": "proj"}]
    assert (
        "a" in props["Notes"]["rich_text"][0]["text"]["content"]
        and "error: b" in props["Notes"]["rich_text"][0]["text"]["content"]
    )


def test_appends_to_open_task(settings):
    seen = []

    def handler(req):
        seen.append((req.method, req.url.path, json.loads(req.content) if req.content else None))
        if req.url.path.endswith("/query"):
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": "open",
                            "properties": {"Notes": {"rich_text": [{"plain_text": "old"}]}},
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"id": "open"})

    assert (
        flags.file(settings, httpx.Client(transport=httpx.MockTransport(handler)), ["new"], [], "d")
        == "open"
    )
    method, path, body = seen[-1]
    assert (method, path) == ("PATCH", "/v1/pages/open")
    assert body["properties"]["Notes"]["rich_text"][0]["text"]["content"].startswith("old\n")


def test_existing_review_task_retains_all_evidence_and_deduplicates(settings):
    settings = settings.model_copy(
        update={"notion_review_page_id": "review", "notion_review_property_id": "notes-id"}
    )
    old = "historical evidence\n" * 200 + "- already recorded"
    writes = []

    def handler(req):
        if req.method == "GET":
            assert req.url.path == "/v1/pages/review"
            return httpx.Response(
                200,
                json={
                    "id": "review",
                    "properties": {
                        "Renamed Notes": {"id": "notes-id", "rich_text": [{"plain_text": old}]}
                    },
                },
            )
        assert req.method == "PATCH" and req.url.path == "/v1/pages/review"
        writes.append(json.loads(req.content))
        return httpx.Response(200, json={"id": "review"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    flags.file(settings, client, ["already recorded", "new finding"], [], "day")
    rich = writes[0]["properties"]["notes-id"]["rich_text"]
    text = "".join(r["text"]["content"] for r in rich)
    assert text.startswith(old) and text.count("already recorded") == 1
    assert "new finding" in text and all(len(r["text"]["content"]) <= 1900 for r in rich)
    old = text
    flags.file(settings, client, ["already recorded", "new finding"], [], "later")
    assert len(writes) == 1
