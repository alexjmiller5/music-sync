"""The hub's read protocol over a fake's in-memory `tables` (lists of rows, or
id -> row dicts): `/v1/cursor`, keyset-paged `/v1/rows/pull` (single and
batched) with the inclusive `hub_at >= since` filter.

Like the hub, it stamps `hub_at` on every row it sees change, so fakes that
write without stamps still move the cursor; a row's own `hub_at` wins when the
test set or changed it. Stored rows are never mutated. `page` caps every page;
`on_page` runs after each page is served, so a test can commit mid-walk.
"""

import json
from datetime import datetime, timedelta, timezone


class HubReads:
    base = "https://hub.test"
    page = 200
    on_page = None

    def served(self, table):
        rows = self.tables.get(table) or []
        stamps = self.__dict__.setdefault("_hub_stamps", {})
        out = []
        for row in rows.values() if isinstance(rows, dict) else rows:
            body = json.dumps(
                {k: v for k, v in row.items() if k != "hub_at"}, sort_keys=True, default=str
            )
            own = row.get("hub_at") or None
            held = stamps.get((table, row["id"]))
            if held is None or held[1] != own:
                stamps[(table, row["id"])] = (body, own, own or self._tick())
            elif held[0] != body:  # changed without a new stamp of its own
                stamps[(table, row["id"])] = (body, own, self._tick())
            out.append({**row, "hub_at": stamps[(table, row["id"])][2]})
        return out

    def _tick(self):
        stamps = self.__dict__.setdefault("_hub_stamps", {})
        latest = max(
            [s[2] for s in stamps.values()]
            + [r.get("hub_at") or "" for t in self.tables for r in self._raw(t)]
            + ["2026-12-01T00:00:00.000Z"]
        )
        later = datetime.fromisoformat(latest.replace("Z", "+00:00")) + timedelta(milliseconds=1)
        return later.astimezone(timezone.utc).isoformat(timespec="milliseconds")[:-6] + "Z"

    def _raw(self, table):
        rows = self.tables.get(table) or []
        return list(rows.values()) if isinstance(rows, dict) else rows

    def read(self, route, body):
        if route == "/v1/cursor":
            marks, at_mark = {}, {}
            for table in body["tables"]:
                stamps = [row["hub_at"] for row in self.served(table)]
                marks[table] = max(stamps, default="")
                at_mark[table] = stamps.count(marks[table]) if marks[table] else 0
            return {"tables": marks, "at_mark": at_mark, "pull_batch": {"items": 4, "rows": 5000}}
        assert route == "/v1/rows/pull"
        if "batch" in body:
            return {"batch": [self.read_page(**pull) for pull in body["batch"]]}
        return self.read_page(**body)

    def read_page(self, table, columns, since="", limit=200, after=None, where=None):
        rows = sorted(
            (
                row
                for row in self.served(table)
                if row["hub_at"] >= (since or "")
                and (after is None or row["id"] > after)
                and all(row.get(k) == v for k, v in (where or {}).items())
            ),
            key=lambda row: row["id"],
        )
        page = rows[: min(limit, self.page)]
        reply = {
            "rows": [{c: row.get(c) for c in columns} for row in page],
            "next_cursor": page[-1]["id"] if len(rows) > len(page) else None,
        }
        if self.on_page:
            self.on_page()
        return reply

    def scan(self, table, columns, *, where=None, since=""):
        rows, after = [], None
        while True:
            page = self.read_page(table, columns, since, after=after, where=where)
            rows.extend(page["rows"])
            if page["next_cursor"] is None:
                return rows
            after = page["next_cursor"]

    def pull(self, table, columns, since="", where=None):
        return self.scan(table, columns, where=where, since=since)
