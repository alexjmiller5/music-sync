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
    "deezer_genres": {"deezer_genres": ["genre"], "deezer_year": None},
    "mb_tags": {"mb_tags": ["tag"], "mb_first_year": None},
}


def input_hash(values):
    # D1 binds JS Number inputs as REAL, including integral numbers.
    values = [float(v) if isinstance(v, (int, float)) else v for v in values]
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
        self.batch_calls = []
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
        if request.method == "GET" and request.url.path == "/v1/catalog":
            from tests.test_metadata_contract import catalog

            return httpx.Response(200, json=catalog())
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
        self.batch_calls.append(body["ids"])
        assert 1 <= len(body["ids"]) <= 50
        assert body.get("col") in SOURCES, "one source per request"
        col = body["col"]
        failures = []
        derived = 0
        for id in body["ids"]:
            self.calls.append((id, col))
            replies = self.replies.get((id, col), [])
            if replies:
                reply = replies.pop(0)
                if isinstance(reply, Exception):
                    raise reply
                if reply.get("failed"):
                    failures.extend(reply["failed"])
                else:
                    derived += reply.get("derived", 0)
                continue
            self.persist(id, col)
            derived += 1
        return httpx.Response(200, json={"derived": derived, "failed": failures})

    def run(self, col=None):
        return backfill_derive.run(self.hub, "songs", col, sleep=lambda _: None, batch_size=1)


def failure(id, col, error="endpoint musicbrainz_isrc returned 502"):
    return {"derived": 0, "failed": [{"id": id, "col": col, "error": error}]}


def test_requests_counts_and_no_other_writes(capsys):
    service = Service(2)
    service.rows["deleted"] = {"id": "deleted", "deleted_at": "t"}
    out = service.run()
    assert service.calls == [(id, col) for col in SOURCES for id in ("S0000", "S0001")]
    assert out["total_recordings"] == out["completed_recordings"] == 2
    assert out["total_sources"] == out["derived"] == out["attempts"] == 6
    assert out["skipped"] == 0 and out["failed"] == []
    assert "recordings 2/2" in capsys.readouterr().out


def test_pending_ids_are_sent_in_batches():
    service = Service(5)
    out = backfill_derive.run(
        service.hub, "songs", "deezer_genres", sleep=lambda _: None, batch_size=2
    )
    assert service.batch_calls == [
        ["S0000", "S0001"],
        ["S0002", "S0003"],
        ["S0004"],
        ["S0000", "S0001"],
        ["S0002", "S0003"],
        ["S0004"],
    ]
    assert out["derived"] == 10 and out["attempts"] == 6


def test_first_year_uses_smaller_batches_than_other_sources():
    service = Service(45)
    out = backfill_derive.run(
        service.hub, "songs", "first_year", sleep=lambda _: None, batch_size=50
    )
    assert [len(batch) for batch in service.batch_calls] == [20, 20, 5]
    assert out["completed_recordings"] == 45 and out["failed"] == []


def test_partial_batch_failure_retries_only_failed_id():
    service = Service(3)
    service.replies[("S0001", "deezer_genres")] = [failure("S0001", "deezer_genres")]
    out = backfill_derive.run(
        service.hub, "songs", "deezer_genres", sleep=lambda _: None, batch_size=3
    )
    assert service.batch_calls[:2] == [["S0000", "S0001", "S0002"], ["S0001"]]
    assert out["failed"] == []
    assert out["derived"] == 6 and out["attempts"] == 3


def test_resume_reuses_source_facts_and_null_year_with_proofs():
    service = Service()
    assert service.run()["completed_recordings"] == 1
    assert service.rows["S0000"]["first_year"] is None
    assert "songs:S0000:title" not in service.proofs
    service.calls.clear()
    second = service.run()
    assert service.calls == []
    assert second["completed_recordings"] == 1 and second["skipped"] == 3
    assert second["derived"] == second["attempts"] == 0


def test_resume_reuses_d1_numeric_year_proof():
    service = Service()
    service.rows["S0000"]["album_year"] = 1990
    service.outputs["deezer_genres"]["deezer_year"] = 1990
    for col in SOURCES:
        service.persist("S0000", col)
    # Literal oracle for the live failure shape, using only synthetic years.
    proof = service.proofs["songs:S0000:first_year"]
    proof["inputs_hash"] = hashlib.sha256(b'["1990.0","1990.0",null]').hexdigest()
    out = service.run()
    assert service.calls == []
    assert out["completed_recordings"] == 1 and out["skipped"] == 3
    assert out["derived"] == out["attempts"] == 0


@pytest.mark.parametrize(
    ("values", "raw", "reuse"),
    [
        ([1990.0, 1990, None], '["1990.0","1990.0",null]', True),
        ([1991, 1990, None], '["1990.0","1990.0",null]', False),
        ([1990, 1990, None], '["1990","1990",null]', False),
        ([None, None, None], "[null,null,null]", True),
        ([None, None, None], '["None",null,null]', False),
        (["01990", "1990", None], '["01990","1990",null]', True),
        (["1990", "1990", None], '["1990.0","1990.0",null]', False),
        ([1e20, 1990, None], '["1.0e+20","1990.0",null]', True),
    ],
)
def test_year_checkpoint_matches_d1_binding(values, raw, reuse):
    service = Service()
    service.rows["S0000"].update(zip(backfill_derive.YEARS, values))
    service.persist("S0000", "first_year")
    service.proofs["songs:S0000:first_year"]["inputs_hash"] = hashlib.sha256(
        raw.encode()
    ).hexdigest()
    out = service.run("first_year")
    assert service.calls == ([] if reuse else [("S0000", "first_year")])
    assert out["skipped"] == int(reuse)
    assert out["derived"] == int(not reuse)
    assert out["completed_recordings"] == 1


def test_partial_failure_resume_refreshes_non_null_year():
    service = Service()
    service.rows["S0000"]["album_year"] = 2000
    service.outputs["mb_tags"] = {"mb_tags": ["fixture"], "mb_first_year": 1980}
    service.persist("S0000", "mb_tags", {"mb_tags": []})
    service.replies[("S0000", "mb_tags")] = [failure("S0000", "mb_first_year")] * 3
    first = service.run()
    assert first["completed_recordings"] == 0 and len(first["failed"]) == 1
    assert "mb_first_year" in json.dumps(first["failed"])
    assert first["derived"] == 2 and first["attempts"] == 5
    assert service.rows["S0000"]["first_year"] == 2000
    service.calls.clear()
    second = service.run()
    assert service.calls == [("S0000", "mb_tags"), ("S0000", "first_year")]
    assert second["completed_recordings"] == 1 and second["failed"] == []
    assert second["skipped"] == 1 and second["derived"] == 2
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
    service.replies[("S0000", "deezer_genres")] = [
        httpx.ReadTimeout("fixture transport failure"),
        failure("S0000", "deezer_genres"),
    ]
    sleeps = []
    out = backfill_derive.run(service.hub, "songs", "deezer_genres", sleep=sleeps.append)
    assert service.calls == [("S0000", "deezer_genres")] * 3 + [("S0000", "first_year")]
    assert sleeps == [5, 15]
    assert out["derived"] == 2 and out["attempts"] == 4 and out["failed"] == []
    assert out["completed_recordings"] == 1


def test_timeout_circuit_only_pauses_failing_source_and_reports_deferred():
    service = Service(100)
    for id in service.rows:
        service.replies[(id, "mb_tags")] = [failure(id, "mb_tags", "TimeoutError")] * 3
    out = service.run()
    counts = Counter(col for _, col in service.calls)
    assert counts == {"deezer_genres": 100, "mb_tags": 15, "first_year": 100}
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


@pytest.mark.parametrize("status", [429, 503])
@pytest.mark.parametrize("retry_after", [0, 6000])
def test_upstream_cooldown_defers_source_without_short_retries(status, retry_after, monkeypatch):
    service = Service(3)
    error = failure("S0000", "deezer_genres", "source temporarily unavailable")
    error["failed"][0].update(status=status, retry_after=retry_after)
    service.replies[("S0000", "deezer_genres")] = [error]
    sleeps = []
    monkeypatch.setattr(backfill_derive.time, "time", lambda: 1000)
    out = backfill_derive.run(service.hub, "songs", None, sleep=sleeps.append, batch_size=1)
    assert sleeps == []
    assert Counter(col for _, col in service.calls) == {
        "deezer_genres": 1,
        "mb_tags": 3,
        "first_year": 3,
    }
    assert out["retry_at"] == {"deezer_genres": 1000 + max(1, retry_after)}
    assert out["stopped_sources"] == ["deezer_genres"]
    assert out["deferred"] == {"deezer_genres": 2}
    assert out["failed"][0]["attempts"] == 1
    assert out["completed_recordings"] == 0
    service.calls.clear()
    resumed = service.run()
    assert resumed["completed_recordings"] == 3
    assert resumed["failed"] == []
    assert service.calls == [
        *((id, "deezer_genres") for id in service.rows),
        *((id, "first_year") for id in service.rows),
    ]


@pytest.mark.parametrize("retry_after", [None, "bad", -1, "NaN", True])
def test_rate_limit_without_valid_delay_still_stops_premature_retries(retry_after, monkeypatch):
    service = Service()
    error = failure("S0000", "deezer_genres", "rate limited")
    error["failed"][0].update(status=429, retry_after=retry_after)
    service.replies[("S0000", "deezer_genres")] = [error]
    monkeypatch.setattr(backfill_derive.time, "time", lambda: 1000)
    out = service.run("deezer_genres")
    assert service.calls == [("S0000", "deezer_genres"), ("S0000", "first_year")]
    assert out["retry_at"] == {"deezer_genres": 1060}


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


def test_spotify_column_rejected_and_album_year_projected():
    with pytest.raises(SystemExit):
        backfill_derive.parse_args(["--col", "title"])
    with pytest.raises(ValueError, match="title"):
        Service().run("title")
    assert "album_year" in backfill_derive.COLS


@pytest.mark.parametrize("values", [{}, {"mb_tags": ["tag"]}])
def test_sparse_success_is_unresolved_and_preserves_observed_fields(values):
    service = Service()
    service.rows["S0000"].update(OUTPUTS["title"])
    before = deepcopy(service.rows["S0000"])
    service.outputs["mb_tags"] = values
    out = service.run("mb_tags")
    assert out["completed_recordings"] == 0
    assert out["failed"][0]["col"] == "mb_tags"
    assert all(service.rows["S0000"][k] == v for k, v in before.items())


def test_count_without_persisted_fields_is_not_completion():
    service = Service()
    service.replies[("S0000", "mb_tags")] = [{"derived": 1}] * 3
    out = service.run("mb_tags")
    assert out["completed_recordings"] == 0
    assert out["failed"][0]["col"] == "mb_tags"


def test_backfill_requires_observed_contract_before_derive(monkeypatch):
    from tests.test_metadata_contract import catalog
    from core.hub import HubError

    service = Service()
    body = catalog()
    body["properties"][0]["derived_by"] = "spotify_isrc"
    monkeypatch.setattr(service.hub, "catalog", lambda: body)
    with pytest.raises(HubError, match="title"):
        service.run()
    assert not service.calls


def test_failed_source_does_not_force_current_year_refresh():
    service = Service()
    service.rows["S0000"]["album_year"] = 2000
    service.persist("S0000", "first_year")
    service.replies[("S0000", "mb_tags")] = [failure("S0000", "mb_tags")] * 3
    out = service.run("mb_tags")
    assert service.calls == [("S0000", "mb_tags")] * 3
    assert out["completed_recordings"] == 0 and out["skipped"] == 1


def test_partial_year_write_is_reread_even_when_group_unresolved():
    service = Service()
    service.rows["S0000"]["album_year"] = 2000
    service.persist("S0000", "first_year")
    service.outputs["mb_tags"] = {"mb_first_year": 1980}
    out = service.run("mb_tags")
    assert out["completed_recordings"] == 0
    assert service.rows["S0000"]["first_year"] == 1980
    assert service.calls[-1] == ("S0000", "first_year")


def test_confirmation_error_preserves_rate_limit(monkeypatch):
    from core.hub import HubError

    service = Service()
    error = failure("S0000", "mb_tags")
    error["failed"][0].update(status=429, retry_after=6000)
    service.replies[("S0000", "mb_tags")] = [error]
    original = service.hub.pull

    def pull(table, cols):
        if service.calls == [("S0000", "mb_tags")] and table == "provenance":
            # Fail confirmation once; later sources must still proceed.
            monkeypatch.setattr(service.hub, "pull", original)
            raise HubError("confirmation unavailable")
        return original(table, cols)

    monkeypatch.setattr(service.hub, "pull", pull)
    out = service.run("mb_tags")
    assert out["failed"][0]["errors"][0]["status"] == 429
    assert "mb_tags" in out["retry_at"]
