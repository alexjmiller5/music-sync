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
        old = "".join(
            t.get("plain_text", "")
            for t in page["properties"].get("Notes", {}).get("rich_text", [])
        )
        body = {
            "properties": {
                "Notes": {"rich_text": [{"text": {"content": (old + "\n" + text)[-1900:]}}]}
            }
        }
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
            "Notes": {"rich_text": [{"text": {"content": text[:1900]}}]},
        },
    }
    r = http.post(f"{NOTION}/v1/pages", headers=_h(settings), json=body)
    r.raise_for_status()
    return r.json()["id"]
