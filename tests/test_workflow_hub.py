import json
import httpx
import pytest
from core.hub import Hub, HubError


def test_scan_exhausts_pages_without_dropping_tombstones():
    seen = []

    def handle(request):
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(
            200,
            json={
                "rows": [{"id": "a", "deleted_at": None}]
                if len(seen) == 1
                else [{"id": "b", "deleted_at": "stamp"}],
                "next_cursor": "a" if len(seen) == 1 else None,
            },
        )

    hub = Hub("https://hub.test", "test", httpx.Client(transport=httpx.MockTransport(handle)))
    assert len(hub.scan("items", ["id", "deleted_at"], where={"label": "Flags"})) == 2
    assert seen[1]["after"] == "a" and all(b["where"] == {"label": "Flags"} for b in seen)


def test_insert_rejects_partial_receipt():
    hub = Hub(
        "https://hub.test",
        "test",
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"inserted": [], "existing": [], "rejected": []})
            )
        ),
    )
    with pytest.raises(HubError):
        hub.insert("items", {"id": "a"})


def test_patch_carries_exact_revision_and_never_uses_push():
    def handle(request):
        assert request.url.path == "/v1/rows/patch"
        assert json.loads(request.content)["expected_revision"] == {
            "updated_at": "r1",
            "hub_at": "h1",
        }
        return httpx.Response(
            200, json={"id": "a", "revision": {"updated_at": "r2", "hub_at": "h2"}}
        )

    hub = Hub("https://hub.test", "test", httpx.Client(transport=httpx.MockTransport(handle)))
    assert (
        hub.patch("items", "a", {"body": "new"}, {"updated_at": "r1", "hub_at": "h1"})["id"] == "a"
    )
