import gzip
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from core import metadata, run, capture, metadata_replay, archive
from core.hub import HubError


def catalog():
    return {
        "tables": [],
        "rules": [],
        "properties": [
            {
                "tbl": "songs",
                "col": col,
                "type": kind,
                "derived_by": None,
                "inputs": None,
                "deleted_at": None,
            }
            for col, kind in [
                ("title", "text"),
                ("artists", "json"),
                ("album", "text"),
                ("album_year", "int"),
                ("duration_ms", "int"),
                ("spotify_ids", "json"),
                ("spotify_playable", "bool"),
            ]
        ],
    }


@pytest.mark.parametrize("mode", ["derived", "missing", "deleted", "valid"])
def test_contract(mode):
    body = catalog()
    if mode == "derived":
        body["properties"][0]["derived_by"] = "spotify_isrc"
    elif mode == "missing":
        body["properties"].pop(0)
    elif mode == "deleted":
        body["properties"][0]["deleted_at"] = "t"
    hub = SimpleNamespace(catalog=lambda: body)
    if mode == "valid":
        metadata.require_observed_contract(hub)
    else:
        with pytest.raises(HubError, match="title"):
            metadata.require_observed_contract(hub)


@pytest.mark.parametrize(
    "path", ["reconcile", "import", "pending", "capture", "replay", "pending_replay"]
)
def test_old_contract_stops_before_mutations(settings, monkeypatch, path):
    body = catalog()
    body["properties"][0]["derived_by"] = "spotify_isrc"
    hub = SimpleNamespace(catalog=lambda: body)
    key, stamp = "raw/spotify-pull/example.json.gz", "2026-01-01T00:00:00.000Z"
    pending = {"writes": False, "planned": [], "operations": []}
    if path == "pending_replay":
        pending.update(intent="metadata_replay", archive_key=key, observed_at=stamp, outcomes=[])
    saved = gzip.compress(json.dumps(pending).encode()) if "pending" in path else None
    monkeypatch.setattr(archive, "get", lambda *args: saved)

    def forbidden(*args, **kwargs):
        pytest.fail("old contract reached mutation or Spotify")

    monkeypatch.setattr(archive, "put", forbidden)
    monkeypatch.setattr(run.flags, "file", forbidden)
    sp = SimpleNamespace(me=forbidden, search_track=forbidden)
    with pytest.raises(HubError, match="title"):
        if "replay" in path:
            metadata_replay.run(settings, key, stamp, dry_run=False, hub=hub)
        elif path == "capture":
            capture.capture(
                {"title": "fixture", "artist": "fixture"},
                sp,
                hub,
                settings,
                datetime.now(timezone.utc),
            )
        else:
            run.reconcile(settings, spotify=sp, hub=hub, writes=path != "import")


@pytest.mark.parametrize("path", ["reconcile", "replay"])
def test_old_contract_preview_available(settings, monkeypatch, path):
    from core.model import Mirror, Live

    body = catalog()
    body["properties"][0]["derived_by"] = "spotify_isrc"
    hub = SimpleNamespace(catalog=lambda: body)
    monkeypatch.setattr(archive, "get", lambda *args: None)
    monkeypatch.setattr(run.mirror, "load_mirror", lambda *args: Mirror({}, {}, {}, [], set()))
    monkeypatch.setattr(run.mirror, "pull_live", lambda *args, **kw: Live({}, {}, {}))
    monkeypatch.setattr(metadata_replay, "_read_live", lambda *args: Live({}, {}, {}))

    def forbidden(*args, **kwargs):
        pytest.fail("preview attempted a write")

    monkeypatch.setattr(archive, "put", forbidden)
    if path == "reconcile":
        out = run.reconcile(
            settings, dry_run=True, hub=hub, spotify=SimpleNamespace(me=lambda: {"id": "fixture"})
        )
        assert not out.errors
    else:
        out = metadata_replay.run(
            settings, "raw/spotify-pull/example.json.gz", "2026-01-01T00:00:00Z", hub=hub
        )
        assert not out["errors"] and out["dry_run"]
