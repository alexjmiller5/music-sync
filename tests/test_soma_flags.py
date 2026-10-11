import copy
import json

import httpx
import pytest

from core import archive, flags
from core.hub import HubError

CONFIG = {
    "table": "items",
    "title": "Review flags",
    "columns": {"title": "label", "status": "state", "notes": "body", "due": "due"},
    "defaults": {"status": "Open"},
    "open_statuses": ["Open", "Doing"],
}


class Server:
    def __init__(self):
        self.rows = {}
        self.writes = []
        self.lose = False
        self.race = None

    def handle(self, request):
        body = json.loads(request.content)
        assert request.url.host == "hub.test"
        if request.url.path == "/v1/rows/pull":
            rows = list(self.rows.values())
            for key, value in body.get("where", {}).items():
                rows = [r for r in rows if r.get(key) == value]
            return httpx.Response(200, json={"rows": rows, "next_cursor": None})
        self.writes.append(copy.deepcopy(body))
        if request.url.path == "/v1/rows/insert":
            row = body["rows"][0]
            existed = row["id"] in self.rows
            self.rows.setdefault(
                row["id"], {**row, "updated_at": "r1", "hub_at": "h1", "deleted_at": None}
            )
            result = {
                "inserted": [] if existed else [row["id"]],
                "existing": [row["id"]] if existed else [],
                "rejected": [],
            }
        elif request.url.path == "/v1/rows/patch":
            row = self.rows[body["id"]]
            if self.race:
                fn, self.race = self.race, None
                fn(row)
            if body["expected_revision"] != {k: row[k] for k in ("updated_at", "hub_at")}:
                return httpx.Response(409, json={"error": "revision_conflict"})
            row.update(body["values"])
            row.update(
                updated_at="r" + str(len(self.writes) + 1), hub_at="h" + str(len(self.writes) + 1)
            )
            result = {"id": row["id"], "revision": {k: row[k] for k in ("updated_at", "hub_at")}}
        else:
            pytest.fail("unexpected mutation " + request.url.path)
        if self.lose:
            self.lose = False
            raise httpx.ReadTimeout("response lost", request=request)
        return httpx.Response(200, json=result)


@pytest.fixture
def setup(settings, monkeypatch):
    saved = {}
    monkeypatch.setattr(archive, "get", lambda settings, key: saved.get(key))
    monkeypatch.setattr(archive, "put", lambda settings, key, value: saved.__setitem__(key, value))
    server = Server()
    settings = settings.model_copy(update={"flags_task_config": CONFIG})
    with httpx.Client(transport=httpx.MockTransport(server.handle)) as client:
        yield settings, server, client, saved


def send(setup, values=None):
    settings, server, http, saved = setup
    return flags.file(settings, http, ["Flag A"] if values is None else values, [], "2026-01-01")


def existing(server, status="Open"):
    server.rows["adopted"] = {
        "id": "adopted",
        "label": "Review flags",
        "state": status,
        "body": "Existing notes",
        "due": "2026-01-01",
        "updated_at": "r1",
        "hub_at": "h1",
        "deleted_at": None,
    }


def test_adopts_open_task_and_appends_full_batch_once(setup):
    _, server, _, _ = setup
    existing(server)
    assert send(setup) == "adopted"
    assert send(setup) == "adopted"
    assert len(server.writes) == 1
    assert server.rows["adopted"]["body"].startswith("Existing notes\n")
    assert server.rows["adopted"]["body"].count("Flag A") == 1


def test_timeout_after_append_recovers_from_retained_marker(setup):
    _, server, _, _ = setup
    existing(server)
    server.lose = True
    with pytest.raises(HubError):
        send(setup)
    assert send(setup) == "adopted"
    assert len(server.writes) == 1 and server.rows["adopted"]["body"].count("Flag A") == 1


def test_concurrent_notes_edit_is_merged_after_definitive_conflict(setup):
    _, server, _, _ = setup
    existing(server)
    server.race = lambda row: row.update(body="Human edit", updated_at="r2", hub_at="h2")
    send(setup)
    assert server.rows["adopted"]["body"].startswith("Human edit\n")
    assert server.rows["adopted"]["body"].count("Flag A") == 1


def test_new_batch_after_closure_creates_new_task_without_reopening_old(setup):
    _, server, _, _ = setup
    existing(server)
    send(setup)
    server.rows["adopted"]["state"] = "Done"
    result = send(setup, ["New flag"])
    assert result != "adopted" and len(server.rows) == 2
    assert server.rows["adopted"]["state"] == "Done"


def test_timeout_after_create_retries_same_identity_preserving_user_edit(setup):
    _, server, _, _ = setup
    server.lose = True
    with pytest.raises(HubError):
        send(setup)
    row = next(iter(server.rows.values()))
    row["body"] = "User replacement"
    row["state"] = "Done"
    assert send(setup) == row["id"]
    assert len(server.rows) == 1 and row["body"] == "User replacement"


def test_ambiguous_append_without_marker_fails_closed_on_newer_revision(setup):
    _, server, _, _ = setup
    existing(server)
    server.lose = True
    with pytest.raises(HubError):
        send(setup)
    server.rows["adopted"].update(body="User removed marker", updated_at="r9", hub_at="h9")
    with pytest.raises(RuntimeError, match="ambiguous"):
        send(setup)
    assert len(server.writes) == 1 and server.rows["adopted"]["body"] == "User removed marker"


def test_pending_batch_is_recovered_even_when_next_run_has_no_flags(setup):
    _, server, _, _ = setup
    existing(server)
    server.lose = True
    with pytest.raises(HubError):
        send(setup)
    assert send(setup, []) == "adopted"
    assert len(server.writes) == 1


def test_nonselected_project_flags_are_not_adopted(setup):
    settings, server, _, _ = setup
    settings.flags_task_config = {
        **CONFIG,
        "columns": {**CONFIG["columns"], "project_ids": "projects"},
        "defaults": {**CONFIG["defaults"], "project_ids": ["owned-project"]},
    }
    existing(server)
    server.rows["adopted"]["projects"] = '["other-project"]'
    result = send(setup)
    assert result != "adopted"
    assert server.rows["adopted"]["body"] == "Existing notes"


def test_multiple_open_candidates_fail_before_any_write(setup):
    _, server, _, saved = setup
    existing(server)
    server.rows["second"] = {**server.rows["adopted"], "id": "second"}
    with pytest.raises(RuntimeError, match="multiple open"):
        send(setup)
    assert not server.writes and saved == {}


def test_life_only_settings_do_not_require_retired_notion_credentials(settings):
    from core.config import Settings

    values = settings.model_dump(
        exclude={"notion_token", "notion_tasks_data_source_id", "notion_project_page_id"}
    )
    configured = Settings(**{**values, "flags_task_config": CONFIG})
    assert configured.notion_token == "" and configured.flags_task_config == CONFIG


REVIEW = {
    "id": "curation-unlike:USAAA2600001",
    "isrc": "USAAA2600001",
    "title": "Song",
    "playlists": ["curated list"],
    "reason": "unliked_while_curated",
    "before_ref": "raw/a",
    "after_ref": "raw/b",
    "prior_like_origin": "observed",
}


def test_review_exception_becomes_one_quiet_task_row(setup):
    from core.soma_flags import deliver_reviews

    settings, server, http, _ = setup
    first = deliver_reviews(settings, http, [REVIEW], "2026-10-09")
    server.rows[first[0]]["state"] = "Doing"  # the owner touched it
    again = deliver_reviews(settings, http, [REVIEW], "2026-10-10")
    assert first == again and len(server.rows) == 1
    row = server.rows[first[0]]
    assert row["label"].startswith("Music Sync review: Song")
    assert "curated list" in row["body"] and row["due"] == "2026-10-09"
    assert row["state"] == "Doing"
    assert deliver_reviews(settings, http, [], "2026-10-10") == []


CAPTURE = {
    "client_id": "client-1",
    "capture_id": "3d2ed84e-9413-4a4a-a7e1-c596201bf84d",
    "received_at": "2026-10-11T18:00:00.000Z",
    "no_match_checks": ["2026-10-11T18:00:05.000Z"],
    "payload": {
        "capture_id": "3d2ed84e-9413-4a4a-a7e1-c596201bf84d",
        "title": "Song",
        "artist": "Artist",
        "apple_music_id": "123",
        "shazam_url": "https://www.shazam.com/track/123/song",
        "isrc": "USAAA2600001",
        "recognized_at": "2026-10-11T13:59:58-04:00",
    },
}
CAPTURE_CONFIG = {
    **CONFIG,
    "columns": {**CONFIG["columns"], "completed_date": "done_on", "ai_completed": "by_agent"},
}


def test_unmatched_capture_opens_one_quiet_task_and_a_later_match_closes_it(setup):
    from core.soma_flags import close_capture_task, open_capture_task

    settings, server, http, _ = setup
    settings = settings.model_copy(update={"capture_task_config": CAPTURE_CONFIG})
    task_id = open_capture_task(settings, http, CAPTURE, "2026-10-11")
    assert open_capture_task(settings, http, CAPTURE, "2026-10-12") == task_id
    assert len(server.rows) == 1
    row = server.rows[task_id]
    assert row["label"] == "Reconcile capture: Song - Artist"
    assert row["state"] == "Open" and row["due"] == "2026-10-11"
    for detail in (
        "https://www.shazam.com/track/123/song",
        "Apple Music ID: 123",
        "ISRC: USAAA2600001",
        "Recognized: 2026-10-11T13:59:58-04:00",
        "Spotify searches: 1",
        "ISRC USAAA2600001",
        "2026-10-11T18:00:05.000Z",
    ):
        assert detail in row["body"], detail
    assert not any("notif" in key for key in row), "no reminder or notification"

    assert close_capture_task(settings, http, task_id, "USAAA2600001", "2026-10-13") is True
    row = server.rows[task_id]
    assert row["state"] == "Completed" and row["done_on"] == "2026-10-13" and row["by_agent"] == 1
    assert row["body"].endswith(
        "2026-10-13: a later check found it on Spotify (USAAA2600001) and added it to the inbox."
    )
    writes = len(server.writes)
    assert close_capture_task(settings, http, task_id, "USAAA2600001", "2026-10-14") is False
    assert len(server.writes) == writes, "a closed task is left alone"


def test_capture_task_keeps_an_owner_closed_task_and_falls_back_to_the_flags_binding(setup):
    from core.soma_flags import close_capture_task, open_capture_task

    settings, server, http, _ = setup  # only flags_task_config is set
    task_id = open_capture_task(
        settings, http, {**CAPTURE, "payload": {**CAPTURE["payload"], "isrc": None}}, "2026-10-11"
    )
    assert "no ISRC from Shazam" in server.rows[task_id]["body"]
    assert "exact title and artist" in server.rows[task_id]["body"]
    server.rows[task_id]["state"] = "Done by hand"
    writes = len(server.writes)
    assert close_capture_task(settings, http, task_id, "USAAA2600001", "2026-10-12") is False
    assert len(server.writes) == writes
    unbound = settings.model_copy(update={"flags_task_config": None, "capture_task_config": None})
    assert open_capture_task(unbound, http, CAPTURE, "2026-10-11") is None


def test_capture_task_binding_reads_its_own_setting_from_the_environment(settings, monkeypatch):
    monkeypatch.setenv("CAPTURE_TASK_CONFIG", json.dumps(CONFIG))
    fresh = type(settings)(**settings.model_dump(exclude={"capture_task_config"}))
    assert fresh.capture_task_config == CONFIG and fresh.flags_task_config is None
