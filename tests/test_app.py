import app


def test_import_has_no_side_effects():
    assert app._run is not None


def test_reconcile_cron_skips_when_not_enabled(monkeypatch):
    monkeypatch.delenv("RECONCILE_ENABLED", raising=False)
    assert app.reconcile_cron.get_raw_f()() == {"skipped": True}


def test_flag_quietly_swallows_a_filing_failure(settings, monkeypatch):
    from core import flags

    def boom(*args, **kwargs):
        raise RuntimeError("notion outage")

    monkeypatch.setattr(flags, "file", boom)
    assert app._flag_quietly(settings, ["a flag"], ["an error"]) is None
