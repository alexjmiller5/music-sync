"""Retained alert batches using the app's existing serialized recovery store.

Identical flags/errors on the same day form one notification batch. Human edits
are merged only after a definitive conflict; unknown patch outcomes require
readback evidence and are never blindly appended again.
"""

import copy
import gzip
import hashlib
import json
from uuid import NAMESPACE_URL, uuid5

from core import archive
from core.hub import Hub, HubError, RevisionConflict


def _rows(hub, binding, where):
    columns = sorted({"id", "deleted_at", "updated_at", "hub_at", *binding["columns"].values()})
    rows = hub.scan(binding["table"], columns, where=where)
    if len({row["id"] for row in rows}) != len(rows):
        raise HubError("task scan changed during pagination")
    return rows


def _read(hub, binding, row_id):
    rows = _rows(hub, binding, {"id": row_id})
    if len(rows) > 1 or (rows and rows[0]["id"] != row_id):
        raise HubError("ambiguous task identity")
    return rows[0] if rows else None


def _new_row(record):
    binding = record["binding"]
    values = {
        **binding["defaults"],
        "title": binding["title"],
        "due": record["today"],
        "notes": record["text"],
    }
    return {
        "id": record["new_id"],
        **{binding["columns"][key]: value for key, value in values.items()},
    }


def _deliver(record, hub, save):
    binding = record["binding"]
    col = binding["columns"]
    if record["delivered"]:
        return record["target"]
    for _ in range(5):
        row = _read(hub, binding, record["target"])
        if record["mode"] == "insert":
            if row is None:
                hub.insert(binding["table"], record["row"])
            record["delivered"] = True
            save()
            return record["target"]
        if row is None:
            raise HubError("retained task target is missing")
        notes = row.get(col["notes"]) or ""
        if record["marker"] in notes:
            record["delivered"] = True
            save()
            return record["target"]
        revision = {key: row[key] for key in ("updated_at", "hub_at")}
        attempt = record.get("attempt")
        if attempt and attempt["revision"] != revision:
            raise HubError("ambiguous prior append: newer row has no retained batch marker")
        if row.get("deleted_at") or row.get(col["status"]) not in binding["open_statuses"]:
            if attempt:
                raise HubError("ambiguous append target is no longer open")
            record.update(mode="insert", target=record["new_id"], row=_new_row(record))
            save()
            continue
        if attempt is None:
            record["attempt"] = {"revision": revision, "notes": notes + "\n" + record["text"]}
            save()  # Retain the exact attempt before crossing the network.
        try:
            hub.patch(
                binding["table"],
                record["target"],
                {col["notes"]: record["attempt"]["notes"]},
                record["attempt"]["revision"],
            )
        except RevisionConflict:
            record["attempt"] = None  # Definitively not applied; re-read and merge.
            save()
            continue
        record["delivered"] = True
        save()
        return record["target"]
    raise HubError("task kept changing; retained batch will retry")


def file_life(settings, http, flags, errors, today):
    endpoint = settings.life_hub_url.rstrip("/")
    key = (
        "music-sync/flag-tasks/"
        + hashlib.sha256(settings.workspace.encode()).hexdigest()
        + ".json.gz"
    )
    raw = archive.get(settings, key)
    state = json.loads(gzip.decompress(raw)) if raw else {"endpoint": endpoint, "batches": {}}
    if state["endpoint"] != endpoint:
        raise ValueError("retained flag tasks belong to another hub")

    def save():
        archive.put(
            settings,
            key,
            gzip.compress(json.dumps(state, ensure_ascii=False, allow_nan=False).encode()),
        )

    hub = Hub(endpoint, settings.life_hub_token, http)
    result = None
    for record in state["batches"].values():
        if not record["delivered"]:
            result = _deliver(record, hub, save)
    if not flags and not errors:
        return result
    identity = json.dumps(
        [settings.workspace, today, flags, errors], ensure_ascii=False, separators=(",", ":")
    )
    digest = hashlib.sha256(identity.encode()).hexdigest()
    if digest not in state["batches"]:
        binding = copy.deepcopy(settings.flags_task_config)
        col = binding["columns"]
        rows = _rows(hub, binding, {col["title"]: binding["title"]})
        candidates = [
            row
            for row in rows
            if not row.get("deleted_at") and row.get(col["status"]) in binding["open_statuses"]
        ]
        if "project_ids" in col and "project_ids" in binding["defaults"]:
            expected = binding["defaults"]["project_ids"]

            def same_project(row):
                value = row.get(col["project_ids"])
                return (json.loads(value) if isinstance(value, str) else value) == expected

            candidates = [row for row in candidates if same_project(row)]
        if len(candidates) > 1:
            raise HubError("multiple open flag tasks require an explicit resolution")
        marker = f"<!-- music-sync-batch:{digest} -->"
        text = (
            today
            + "\n"
            + "\n".join(
                [f"- {value}" for value in flags] + [f"- error: {value}" for value in errors]
            )
            + "\n"
            + marker
        )
        row_id = uuid5(NAMESPACE_URL, "music-sync-flags-v1:" + digest).hex
        record = {
            "binding": binding,
            "marker": marker,
            "text": text,
            "today": today,
            "new_id": row_id,
            "target": candidates[0]["id"] if candidates else row_id,
            "mode": "append" if candidates else "insert",
            "attempt": None,
            "delivered": False,
        }
        if not candidates:
            record["row"] = _new_row(record)
        state["batches"][digest] = record
        save()
    return _deliver(state["batches"][digest], hub, save)


def deliver_reviews(settings, http, items, today):
    """One create-only task row per review exception; an existing row stays untouched.

    The row identity comes from the exception itself, so repeated runs, retries and
    a row the owner already closed or deleted never produce a second task.
    """
    binding = settings.flags_task_config
    if not binding or not items:
        return []
    hub = Hub(settings.life_hub_url.rstrip("/"), settings.life_hub_token, http)
    col = binding["columns"]
    ids = []
    for item in items:
        identity = json.dumps(
            [settings.workspace, item["id"], item.get("before_ref")], separators=(",", ":")
        )
        row_id = uuid5(NAMESPACE_URL, "music-sync-review-v1:" + identity).hex
        values = {
            **binding["defaults"],
            "title": f"Music Sync review: {item.get('title') or item['isrc']} was unliked "
            "while still curated",
            "due": today,
            "notes": "\n".join(
                [
                    f"Recording {item['isrc']} was liked and curated, then unliked while it "
                    "stayed curated. Curated membership is kept and it is not re-liked.",
                    "Playlists: " + ", ".join(item.get("playlists") or []),
                    f"Prior like origin: {item.get('prior_like_origin')}",
                    f"Before: {item.get('before_ref')}",
                    f"After: {item.get('after_ref')}",
                    "Re-like it in Spotify to keep it, or remove it from the curated "
                    "playlists to let it go.",
                ]
            ),
        }
        hub.insert(binding["table"], {"id": row_id, **{col[k]: v for k, v in values.items()}})
        ids.append(row_id)
    return ids
