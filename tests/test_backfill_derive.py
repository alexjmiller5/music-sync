import hashlib
import importlib.util
import json
import sqlite3
import sys
from collections import Counter
from copy import deepcopy
from pathlib import Path

import httpx
import pytest

from core.hub import Hub

spec = importlib.util.spec_from_file_location(
    "backfill_derive", Path(__file__).parents[1] / "scripts" / "backfill_derive.py"
)
backfill_derive = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backfill_derive)

SOURCES = {
    "title": "spotify_isrc",
    "deezer_genres": "deezer_isrc",
    "mb_tags": "musicbrainz_isrc",
    "first_year": "first_year",
}
OUTPUTS = {
    "title": {
        "title": "Fixture",
        "artists": ["Fixture artist"],
        "album": "Fixture album",
        "album_year": 2000,
        "duration_ms": 120000,
        "spotify_ids": ["fixture-track"],
        "spotify_playable": 1,
    },
    "deezer_genres": {"deezer_genres": [], "deezer_year": None},
    "mb_tags": {"mb_tags": [], "mb_first_year": None},
}


def input_hash(values):
    # Independent oracle: the hub hashes SQLite's text rendering of inputs.
    db = sqlite3.connect(":memory:")
    try:
        raw = db.execute(
            "SELECT json_array(" + ",".join("CAST(? AS TEXT)" for _ in values) + ")", values
        ).fetchone()[0]
        return hashlib.sha256(raw.encode()).hexdigest()
    finally:
        db.close()


class Service:
    """HTTP boundary double with persisted proofs, including partial writes."""

    def __init__(self, n=1):
        self.rows = {f"S{i:04}": {"id": f"S{i:04}", "deleted_at": None} for i in range(n)}
        self.proofs = {}
        self.calls = []
        self.replies = {}
        self.outputs = deepcopy(OUTPUTS)
        self.hub = Hub(
            "https://hub.test",
            "dummy",
            http=httpx.Client(transport=httpx.MockTransport(self.respond)),
        )

    def persist(self, id, col, values=None):
        row = self.rows[id]
        inputs = [row.get(c) for c in ("album_year", "deezer_year", "mb_first_year")]
        if values is None:
            if col == "first_year":
                years = [v for v in inputs if v is not None]
                values = {"first_year": min(years) if years else None}
            else:
                values = self.outputs[col]
        digest = input_hash(inputs if col == "first_year" else [id])
        row.update(deepcopy(values))
        for field in values:
            key = f"songs:{id}:{field}"
            self.proofs[key] = {
                "id": key,
                "from_kind": f"http:{SOURCES[col]}",
                "rel": "derived_from",
                "asserted_by": "hub",
                "inputs_hash": digest,
                "deleted_at": None,
            }

    def respond(self, request):
        # Any Spotify request, row push, or unrelated API is a test failure.
        assert request.url.host == "hub.test" and request.method == "POST"
        assert request.url.path in ("/v1/rows/pull", "/v1/derive")
        body = json.loads(request.content)
        if request.url.path == "/v1/rows/pull":
            assert body["table"] in ("songs", "provenance")
            rows = self.rows.values() if body["table"] == "songs" else self.proofs.values()
            return httpx.Response(
                200, json={"rows": [{c: r.get(c) for c in body["columns"]} for r in rows]}
            )
        assert body["table"] == "songs"
        assert len(body["ids"]) == 1, "one recording per request"
        assert body.get("col") in SOURCES, "one source per request"
        id, col = body["ids"][0], body["col"]
        self.calls.append((id, col))
        replies = self.replies.get((id, col), [])
        if replies:
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return httpx.Response(200, json=reply)
        self.persist(id, col)
        return httpx.Response(200, json={"derived": 1, "failed": []})

    def run(self, col=None):
        return backfill_derive.run(self.hub, "songs", col, sleep=lambda _: None)


def failure(id, col, error="endpoint musicbrainz_isrc returned 502"):
    return {"derived": 0, "failed": [{"id": id, "col": col, "error": error}]}


def test_requests_counts_and_no_other_writes(capsys):
    service = Service(2)
    service.rows["deleted"] = {"id": "deleted", "deleted_at": "t"}
    out = service.run()
    assert service.calls == [(id, col) for id in ("S0000", "S0001") for col in SOURCES]
    assert out["total_recordings"] == out["completed_recordings"] == 2
    assert out["total_sources"] == out["derived"] == out["attempts"] == 8
    assert out["skipped"] == 0 and out["failed"] == []
    assert "recordings 2/2" in capsys.readouterr().out


def test_resume_accepts_no_match_and_null_year_with_proofs():
    service = Service()
    service.outputs["title"] = {"spotify_ids": [], "spotify_playable": 0}
    assert service.run()["completed_recordings"] == 1
    assert service.rows["S0000"]["first_year"] is None
    assert "songs:S0000:title" not in service.proofs
    service.calls.clear()
    second = service.run()
    assert service.calls == []
    assert second["completed_recordings"] == 1 and second["skipped"] == 4
    assert second["derived"] == second["attempts"] == 0


def test_partial_failure_resume_refreshes_non_null_year():
    service = Service()
    service.outputs["mb_tags"] = {"mb_tags": ["fixture"], "mb_first_year": 1980}
    service.persist("S0000", "mb_tags", {"mb_tags": []})
    service.replies[("S0000", "mb_tags")] = [failure("S0000", "mb_first_year")] * 3
    first = service.run()
    assert first["completed_recordings"] == 0 and len(first["failed"]) == 1
    assert "mb_first_year" in json.dumps(first["failed"])
    assert first["derived"] == 3 and first["attempts"] == 6
    assert service.rows["S0000"]["first_year"] == 2000
    service.calls.clear()
    second = service.run()
    assert service.calls == [("S0000", "mb_tags"), ("S0000", "first_year")]
    assert second["completed_recordings"] == 1 and second["failed"] == []
    assert second["skipped"] == second["derived"] == 2
    assert service.rows["S0000"]["first_year"] == 1980


@pytest.mark.parametrize("mode", ["stale_year", "deleted_proof", "wrong_hash", "wrong_source"])
def test_proof_identity_and_year_inputs_on_restart(mode):
    service = Service()
    for col in SOURCES:
        service.persist("S0000", col)
    if mode == "stale_year":
        service.persist("S0000", "mb_tags", {"mb_tags": [], "mb_first_year": 1970})
        expected = [("S0000", "first_year")]
    else:
        proof = service.proofs["songs:S0000:deezer_year"]
        proof[
            {
                "deleted_proof": "deleted_at",
                "wrong_hash": "inputs_hash",
                "wrong_source": "from_kind",
            }[mode]
        ] = "wrong"
        expected = [("S0000", "deezer_genres"), ("S0000", "first_year")]
    assert service.run()["completed_recordings"] == 1
    assert service.calls == expected


def test_transport_and_returned_failures_retry_without_double_counting():
    service = Service()
    service.replies[("S0000", "title")] = [
        httpx.ReadTimeout("fixture transport failure"),
        failure("S0000", "title"),
    ]
    sleeps = []
    out = backfill_derive.run(service.hub, "songs", "title", sleep=sleeps.append)
    assert service.calls == [("S0000", "title")] * 3 + [("S0000", "first_year")]
    assert sleeps == [5, 15]
    assert out["derived"] == 2 and out["attempts"] == 4 and out["failed"] == []
    assert out["completed_recordings"] == 1


def test_timeout_circuit_only_pauses_failing_source_and_reports_deferred():
    service = Service(100)
    for id in service.rows:
        service.replies[(id, "mb_tags")] = [failure(id, "mb_tags", "TimeoutError")] * 3
    out = service.run()
    counts = Counter(col for _, col in service.calls)
    assert counts == {"title": 100, "deezer_genres": 100, "mb_tags": 15, "first_year": 100}
    assert out["completed_recordings"] == 0 and len(out["failed"]) == 5
    assert out["stopped_sources"] == ["mb_tags"]
    assert out["deferred"] == {"mb_tags": 95}


def test_record_specific_failures_continue_and_success_resets_timeout_streak():
    service = Service(12)
    for i, id in enumerate(service.rows):
        if i != 4:
            reason = "TimeoutError" if i < 4 else "endpoint musicbrainz_isrc returned 502"
            service.replies[(id, "mb_tags")] = [failure(id, "mb_tags", reason)] * 3
    out = service.run()
    assert out["stopped_sources"] == [] and len(out["failed"]) == 11
    assert out["completed_recordings"] == 1
    assert ("S0011", "first_year") in service.calls


def test_empty_success_is_failure_and_cli_exits_nonzero(monkeypatch, capsys):
    service = Service()
    service.replies[("S0000", "first_year")] = [{"derived": 0, "failed": []}] * 3
    monkeypatch.setenv("LIFE_HUB_URL", "https://hub.test")
    monkeypatch.setenv("LIFE_HUB_TOKEN", "dummy")
    monkeypatch.setattr("core.hub.Hub", lambda *args: service.hub)
    monkeypatch.setattr(backfill_derive.time, "sleep", lambda _: None)
    monkeypatch.setattr(sys, "argv", ["backfill_derive.py", "--col", "first_year"])
    assert backfill_derive.main() == 1
    summary = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert summary["completed_recordings"] == 0 and len(summary["failed"]) == 1
    assert len(service.calls) == 3


def test_invalid_column_rejected_before_http():
    with pytest.raises(SystemExit) as exc:
        backfill_derive.parse_args(["--col", "not_a_source"])
    assert exc.value.code == 2
