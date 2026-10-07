import pytest
import httpx

from core import history
from core.hub import Hub, HubError
from tests.test_release_regressions import raw


def test_occurrence_ids_are_stable_per_source_locator_with_unknown_dates():
    source = "raw/spotify-pull/source.json.gz"
    body = {"items": {"P/~": [raw(added=None), raw(added=None)]}}
    acts, stamps = history.occurrence_evidence(body, source, {})
    assert len({a.row["id"] for a in acts}) == 2
    assert acts[0].row["detail"]["locator"] == "/items/P~1~0/0"
    assert history.occurrence_evidence(body, source, {}) == (acts, stamps)
    assert history.occurrence_evidence(body, source, stamps)[0] == []
    assert "added_at" not in acts[0].row["detail"]


@pytest.mark.parametrize(
    "receipt",
    [
        {"inserted": ["a"], "existing": ["b"], "rejected": []},
        {"inserted": ["a"], "existing": [], "rejected": []},
        {"inserted": ["a"], "existing": ["a"], "rejected": []},
        {"inserted": [], "existing": [], "rejected": [{"id": "b"}]},
    ],
)
def test_insert_accounts_for_every_id_without_overwriting(receipt):
    import json

    sent = []

    def handler(request):
        assert request.url.path == "/v1/rows/insert"
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=receipt)

    hub = Hub("https://hub.test", "dummy", httpx.Client(transport=httpx.MockTransport(handler)))
    rows = [
        {"id": "a", "updated_at": "2026-01-01T00:00:00.000Z"},
        {"id": "b", "updated_at": "2026-01-01T00:00:00.000Z"},
    ]
    if receipt["existing"] == ["b"]:
        assert hub.insert_rows("provenance", rows) == receipt
    else:
        with pytest.raises(HubError):
            hub.insert_rows("provenance", rows)
    assert sent == [{"table": "provenance", "columns": ["id", "updated_at"], "rows": rows}]


def test_catalog_pull_exhausts_keyset_pages_and_preserves_revisions():
    import json

    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(
            200,
            json={
                "rows": [{"id": "b", "updated_at": "second", "hub_at": "second"}],
                "next_cursor": None,
            }
            if body.get("after")
            else {
                "rows": [{"id": "a", "updated_at": "first", "hub_at": "first"}],
                "next_cursor": "next",
            },
        )

    hub = Hub("https://hub.test", "dummy", httpx.Client(transport=httpx.MockTransport(handler)))
    rows = hub.pull("songs", ["id", "updated_at", "hub_at"])
    assert [r["id"] for r in rows] == ["a", "b"]
    assert requests[0]["limit"] == 200 and requests[1]["after"] == "next"
