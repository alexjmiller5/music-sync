import json

import httpx
import pytest

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


@pytest.mark.parametrize("existing", [False, True])
def test_long_evidence_is_preserved_in_full(settings, existing):
    written = []
    old = "original evidence\n" * 500
    new = "new evidence\n" * 500

    def handler(req):
        if req.url.path.endswith("/query"):
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": "open",
                            "properties": {"Notes": {"rich_text": [{"plain_text": old}]}},
                        }
                    ]
                    if existing
                    else []
                },
            )
        written.append(json.loads(req.content))
        return httpx.Response(200, json={"id": "open"})

    flags.file(settings, httpx.Client(transport=httpx.MockTransport(handler)), [new], [], "today")
    chunks = written[0]["properties"]["Notes"]["rich_text"]
    combined = "".join(c["text"]["content"] for c in chunks)
    prefix = (old + "\n" if existing else "") + "today [music-sync-batch:"
    assert combined.startswith(prefix) and combined.endswith("]\n- " + new)
    assert len(combined) == len(prefix) + 16 + len("]\n- " + new)
    assert all(len(c["text"]["content"]) <= 1900 for c in chunks)


def test_oversized_flag_receipt_fails_before_overwriting_existing_evidence(settings):
    calls = []

    def handler(req):
        calls.append(req.method)
        assert req.url.path.endswith("/query")
        return httpx.Response(200, json={"results": []})

    with pytest.raises(ValueError, match="capacity"):
        flags.file(
            settings,
            httpx.Client(transport=httpx.MockTransport(handler)),
            ["x" * 200000],
            [],
            "today",
        )
    assert calls == ["POST"]


def test_retry_after_lost_append_does_not_append_twice(settings):
    notes = {"text": "old"}
    patches = []

    def handler(req):
        if req.url.path.endswith("/query"):
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": "open",
                            "properties": {"Notes": {"rich_text": [{"plain_text": notes["text"]}]}},
                        }
                    ]
                },
            )
        body = json.loads(req.content)
        patches.append(body)
        notes["text"] = "".join(
            part["text"]["content"] for part in body["properties"]["Notes"]["rich_text"]
        )
        if len(patches) == 1:
            raise httpx.ReadTimeout("response lost", request=req)
        return httpx.Response(200, json={"id": "open"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(httpx.ReadTimeout):
        flags.file(settings, client, ["same flag"], [], "2026-10-09")
    assert flags.file(settings, client, ["same flag"], [], "2026-10-09") == "open"
    assert len(patches) == 1 and notes["text"].count("same flag") == 1
    flags.file(settings, client, ["next flag"], [], "2026-10-09")
    assert len(patches) == 2
