"""Recognition history: retained originals, create-only rows, idempotent projection."""

import gzip
import json
from datetime import datetime, timezone

import pytest

from core import capture, recognition
from tests.test_capture import INBOX, FakeHub, FakeSpotify, track

NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)


@pytest.fixture
def objects(monkeypatch):
    store = {}
    monkeypatch.setattr(recognition.archive, "get", lambda s, k: store.get(k))
    monkeypatch.setattr(recognition.archive, "put", lambda s, k, v: store.__setitem__(k, v))
    monkeypatch.setattr(capture.archive, "get", lambda s, k: store.get(k))
    monkeypatch.setattr(capture.archive, "put", lambda s, k, v: store.__setitem__(k, v))
    return store


class StoringHub(FakeHub):
    def catalog(self):
        cat = super().catalog()
        extra = [{"tbl": "songs", "col": col} for col in recognition.PROJECTION]
        return {**cat, "properties": [*cat["properties"], *extra]}

    def insert_rows(self, table, rows):
        ids = {r["id"] for r in self.tables[table]}
        new = [r for r in rows if r["id"] not in ids]
        self.tables[table].extend(new)
        return {
            "inserted": [r["id"] for r in new],
            "existing": [r["id"] for r in rows if r["id"] in ids],
            "rejected": [],
        }


def songs_pushed(hub):
    return [row for table, rows in hub.pushed if table == "songs" for row in rows]


def test_capture_projection_counts_each_event_once(settings, objects):
    sp, hub = FakeSpotify([track("t", "Song", "Artist")]), StoringHub(INBOX)
    payload = {"capture_id": "one", "title": "Song", "artist": "Artist"}
    capture.capture(payload, sp, hub, settings, NOW, client_id="phone")
    capture.capture(payload, sp, hub, settings, NOW, client_id="phone")  # retry
    first = songs_pushed(hub)[-1]
    assert first["shazamed"] == 1 and first["shazam_count"] == 1
    assert first["shazam_dates_estimated"] == 1  # receipt time only
    assert first["shazam_first_at"] == "2026-10-09T12:00:00.000Z"
    exact = {
        "capture_id": "two",
        "title": "Song",
        "artist": "Artist",
        "recognized_at": "2026-10-10T08:00:00-04:00",
    }
    capture.capture(exact, sp, hub, settings, NOW, client_id="phone")
    last = songs_pushed(hub)[-1]
    assert last["shazam_count"] == 2
    assert last["shazam_last_at"] == "2026-10-10T12:00:00.000Z"
    assert last["shazam_dates_estimated"] == 1  # one event still estimated


def test_exact_recognition_alone_is_not_estimated():
    row = recognition.project("I", [{"recognized_at": "2026-01-01T00:00:00Z"}])
    assert row["shazam_dates_estimated"] == 0 and row["shazam_count"] == 1
    unknown = recognition.project("I", [{}])
    assert unknown["shazam_first_at"] is None and unknown["shazam_dates_estimated"] == 1


def history(n=2, isrc="USAAA2600001"):
    return [
        {
            "evidence_id": f"shazam-history:{i}",
            "isrc": isrc,
            "estimated_recognized_at": f"2024-05-1{i}T21:37:34Z",
            "estimate_basis": "playlist_added_at_estimate",
            "source_ref": "raw/spotify-pull/baseline.json.gz",
            "source_locator": f"/items/P/{i}",
        }
        for i in range(n)
    ]


def test_history_import_is_create_only_and_projects_estimated_dates(settings, objects):
    hub = StoringHub(INBOX)
    out = recognition.import_history(settings, hub, history(), NOW)
    again = recognition.import_history(settings, hub, history(), NOW)
    assert (out["inserted"], again["inserted"], again["existing"]) == (2, 0, 2)
    rows = recognition.projections(settings, hub.tables["provenance"])
    assert rows == [
        {
            "id": "USAAA2600001",
            "shazamed": 1,
            "shazam_count": 2,
            "shazam_first_at": "2024-05-10T21:37:34.000Z",
            "shazam_last_at": "2024-05-11T21:37:34.000Z",
            "shazam_dates_estimated": 1,
        }
    ]
    event = json.loads(gzip.decompress(objects[hub.tables["provenance"][0]["from_ref"]]))["event"]
    assert event["recognized_at"] is None and event["source_locator"] == "/items/P/0"


def test_changed_history_original_is_refused(settings, objects):
    hub = StoringHub(INBOX)
    recognition.import_history(settings, hub, history(1), NOW)
    changed = history(1)
    changed[0]["estimated_recognized_at"] = "2020-01-01T00:00:00Z"
    with pytest.raises(ValueError, match="differs"):
        recognition.import_history(settings, hub, changed, NOW)


def test_legacy_capture_edges_become_estimated_events_but_playlist_imports_do_not():
    rows = [
        {"id": "shazam:I1:I1", "from_ref": "I1", "to_ref": "I1", "created_at": "x"},
        {
            "id": "shazam:123:I2",
            "from_ref": "123",
            "to_ref": "I2",
            "created_at": "2026-10-02T23:13:12.321Z",
        },
        {
            "id": "shazam:9:I3",
            "from_ref": "9",
            "to_ref": "I3",
            "created_at": "y",
            "deleted_at": "z",
        },
    ]
    events = recognition.legacy_capture_events(rows)
    assert [e["isrc"] for e in events] == ["I2"]
    assert events[0]["estimated_recognized_at"] == "2026-10-02T23:13:12.321Z"


def test_capture_without_summary_columns_still_records_the_event(settings, objects):
    hub = StoringHub(INBOX)
    hub.catalog = lambda: FakeHub.catalog(hub)
    sp = FakeSpotify([track("t", "Song", "Artist")])
    out = capture.capture(
        {"capture_id": "x", "title": "Song", "artist": "Artist"}, sp, hub, settings, NOW
    )
    assert out["ok"] and len(hub.tables["provenance"]) == 1
    assert all("shazamed" not in row for row in songs_pushed(hub))
