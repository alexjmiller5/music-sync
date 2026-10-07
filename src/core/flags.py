"""Batch a run's flags into ONE open Notion Chore task, appending while it stays open."""

import httpx

from core.config import Settings

TITLE = "Music Sync flags"
NOTION = "https://api.notion.com"
VERSION = "2026-03-11"


def _h(settings: Settings) -> dict:
    return {
        "Authorization": f"Bearer {settings.notion_token}",
        "Notion-Version": VERSION,
        "Content-Type": "application/json",
    }


def file(
    settings: Settings, http: httpx.Client, flags: list[str], errors: list[str], today: str
) -> str | None:
    lines = [f"- {f}" for f in flags] + [f"- error: {e}" for e in errors]
    if not lines:
        return None
    text = f"{today}\n" + "\n".join(lines)
    if settings.notion_review_page_id:
        if not settings.notion_review_property_id:
            raise ValueError("review task Notes property ID is required")
        q = http.get(f"{NOTION}/v1/pages/{settings.notion_review_page_id}", headers=_h(settings))
        q.raise_for_status()
        results = [q.json()]
    else:
        q = http.post(
            f"{NOTION}/v1/data_sources/{settings.notion_tasks_data_source_id}/query",
            headers=_h(settings),
            json={
                "filter": {
                    "and": [
                        {"property": "Name", "title": {"equals": TITLE}},
                        {"property": "Status", "status": {"does_not_equal": "Completed"}},
                    ]
                },
                "page_size": 1,
            },
        )
        q.raise_for_status()
        results = q.json().get("results") or []
    if results:
        page = results[0]
        prop_id = settings.notion_review_property_id or "Notes"
        prop = next(
            (v for k, v in page["properties"].items() if v.get("id") == prop_id or k == prop_id),
            None,
        )
        if prop is None:
            raise ValueError("review task Notes property is missing")
        if prop.get("has_more"):
            raise ValueError("review property is truncated; refusing to overwrite evidence")
        old = "".join(
            t.get("plain_text", t.get("text", {}).get("content", ""))
            for t in prop.get("rich_text", [])
        )
        known = set(old.splitlines())
        new = list(dict.fromkeys(line for line in lines if line not in known))
        if not new:
            return page["id"]
        text = old + "\n" + today + "\n" + "\n".join(new)
        body = {"properties": {prop_id: {"rich_text": _rich_text(text)}}}
        r = http.patch(f"{NOTION}/v1/pages/{page['id']}", headers=_h(settings), json=body)
        r.raise_for_status()
        return page["id"]
    body = {
        "parent": {
            "type": "data_source_id",
            "data_source_id": settings.notion_tasks_data_source_id,
        },
        "properties": {
            "Name": {"title": [{"text": {"content": TITLE}}]},
            "Status": {"status": {"name": "To Do"}},
            "Priority": {"select": {"name": "High"}},
            "Due Date": {"date": {"start": today}},
            "Tags": {"multi_select": [{"name": "Chore"}]},
            "Project": {"relation": [{"id": settings.notion_project_page_id}]},
            "Notes": {"rich_text": _rich_text(text)},
        },
    }
    r = http.post(f"{NOTION}/v1/pages", headers=_h(settings), json=body)
    r.raise_for_status()
    return r.json()["id"]


def _rich_text(text: str) -> list[dict]:
    parts = [{"text": {"content": text[i : i + 1900]}} for i in range(0, len(text), 1900)]
    if len(parts) > 100:
        raise ValueError("review text exceeds property capacity; refusing to discard evidence")
    return parts
