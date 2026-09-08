import importlib.util
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "backfill_derive", Path(__file__).parents[1] / "scripts" / "backfill_derive.py"
)
backfill_derive = importlib.util.module_from_spec(spec)
sys.modules["backfill_derive"] = backfill_derive
spec.loader.exec_module(backfill_derive)


def test_select_ids_filters_null_col_and_deleted():
    rows = [
        {"id": "A", "first_year": None, "deleted_at": None},
        {"id": "B", "first_year": 1999, "deleted_at": None},
        {"id": "C", "first_year": None, "deleted_at": "t"},
    ]
    assert backfill_derive.select_ids(rows, "first_year") == ["A"]


class FakeHub:
    def __init__(self, n):
        self.n = n
        self.calls = []

    def pull(self, table, columns):
        return [{"id": f"S{i}", "first_year": None, "deleted_at": None} for i in range(self.n)]

    def derive(self, table, ids):
        self.calls.append(list(ids))
        return {"derived": len(ids), "failed": []}


def test_run_chunks_by_50_and_sleeps_between_but_not_after_last():
    hub = FakeHub(120)
    sleeps = []
    out = backfill_derive.run(hub, "songs", "first_year", sleep=sleeps.append)
    assert [len(c) for c in hub.calls] == [50, 50, 20]
    assert sleeps == [60, 60]
    assert out == {"derived": 120, "failed": []}


def test_run_reports_failed_ids():
    class FailingHub(FakeHub):
        def derive(self, table, ids):
            self.calls.append(list(ids))
            return {"derived": len(ids) - 1, "failed": [ids[0]]}

    hub = FailingHub(3)
    out = backfill_derive.run(hub, "songs", "first_year", sleep=lambda s: None)
    assert out["failed"] == ["S0"]
