"""soma hub client over the HTTP protocol. Knows a URL and a bearer token, nothing else."""

import time
from collections import defaultdict
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

PUSH_CHUNK = 500
READ_RETRY_DELAYS = (2, 5)  # transient hub/D1 failures on idempotent page reads
DERIVE_CHUNK = 50
USER_AGENT = "music-sync/0.1 (+https://github.com/alexjmiller5/music-sync)"


class HubError(RuntimeError):
    def __init__(self, message, *, category="hub", status=None, route=None):
        super().__init__(message)
        self.diagnostic = {"category": category}
        self.route = route
        if status is not None:
            self.diagnostic["status"] = status


class RevisionConflict(HubError):
    pass


def with_read_retries(hub):
    """The serialized worker's clients retry transient failures on idempotent page reads."""
    hub.read_retries = READ_RETRY_DELAYS
    return hub


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Hub:
    def __init__(
        self, base_url: str, token: str, http: httpx.Client | None = None, read_retries=()
    ):
        self.read_retries = tuple(read_retries)
        self.base = base_url.rstrip("/")
        self._http = http or httpx.Client(timeout=120)
        self._headers = {"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT}

    def _post(self, route: str, body: dict) -> dict:
        try:
            r = self._http.post(f"{self.base}{route}", json=body, headers=self._headers)
        except httpx.HTTPError as e:
            raise HubError(
                f"hub unreachable: {type(e).__name__}", category="transport", route=route
            ) from e
        if r.status_code >= 400:
            if r.status_code == 409:
                raise RevisionConflict("hub row revision changed")
            raise HubError(
                f"hub HTTP {r.status_code}: {r.text[:300]}",
                category="http",
                status=r.status_code,
                route=route,
            )
        return r.json()

    def pull(
        self, table: str, columns: list[str], since: str = "", where: dict | None = None
    ) -> list[dict]:
        return self.scan(table, columns, where=where, since=since)

    def scan(self, table, columns, *, where=None, since=""):
        body = {"table": table, "columns": columns, "since": since, "limit": 200}
        if where is not None:
            body["where"] = where
        rows, seen = [], set()
        while True:
            page = self._read_page(body)
            if not isinstance(page.get("rows"), list) or "next_cursor" not in page:
                raise HubError("incomplete scan receipt", category="pagination_receipt")
            rows.extend(page["rows"])
            cursor = page["next_cursor"]
            if cursor is None:
                return rows
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise HubError("invalid scan cursor", category="pagination_cursor")
            seen.add(cursor)
            body["after"] = cursor

    def pull_many(self, table, columns, key, values, where):
        """Exhaust disjoint equality slices through the existing four-item read batch."""
        pending = [
            {
                "table": table,
                "columns": columns,
                "since": "",
                "limit": 200,
                "where": {**where, key: value},
            }
            for value in dict.fromkeys(values)
        ]
        rows, identities, cursors = [], set(), {}
        while pending:
            batch, pending = pending[:4], pending[4:]
            result = self.read("/v1/rows/pull", {"batch": batch})
            pages = result.get("batch") if isinstance(result, dict) else None
            if not isinstance(pages, list) or len(pages) != len(batch):
                raise HubError("incomplete batch receipt", category="pagination_receipt")
            for request, page in zip(batch, pages, strict=True):
                if (
                    not isinstance(page, dict)
                    or not isinstance(page.get("rows"), list)
                    or "next_cursor" not in page
                ):
                    raise HubError("incomplete slice receipt", category="pagination_receipt")
                for row in page["rows"]:
                    if (
                        not isinstance(row, dict)
                        or not isinstance(row.get("id"), str)
                        or not set(columns) <= row.keys()
                        or row["id"] in identities
                    ):
                        raise HubError("invalid slice row", category="pagination_receipt")
                    identities.add(row["id"])
                    rows.append(row)
                cursor = page["next_cursor"]
                if cursor is not None:
                    seen = cursors.setdefault(request["where"][key], set())
                    if (
                        not isinstance(cursor, str)
                        or not cursor
                        or cursor in seen
                        or not page["rows"]
                    ):
                        raise HubError("invalid slice cursor", category="pagination_cursor")
                    seen.add(cursor)
                    pending.append({**request, "after": cursor})
        return rows

    def supports_queries(self) -> bool:
        try:
            response = self._http.get(f"{self.base}/v1/session", headers=self._headers)
        except httpx.HTTPError as exc:
            raise HubError("hub session unavailable", route="/v1/session") from exc
        if response.status_code >= 400:
            raise HubError(
                "hub session unavailable",
                category="http",
                status=response.status_code,
                route="/v1/session",
            )
        return response.json().get("capabilities", {}).get("row_query") == "bounded-v1"

    def query(self, table, columns, filter=None):
        """Exhaust a bounded predicate query; malformed/overlapping pages stop delivery."""
        if "id" not in columns:
            raise ValueError("query projection must include id")
        body = {
            "table": table,
            "columns": columns,
            "limit": 200,
            "order": [{"column": "id", "direction": "asc"}],
        }
        if filter is not None:
            body["filter"] = filter
        rows, cursors, identities = [], set(), set()
        while True:
            page = self.read("/v1/rows/query", body)
            if (
                not isinstance(page, dict)
                or not isinstance(page.get("rows"), list)
                or "next_cursor" not in page
            ):
                raise HubError("incomplete query receipt", category="pagination_receipt")
            for row in page["rows"]:
                if (
                    not isinstance(row, dict)
                    or not isinstance(row.get("id"), str)
                    or not set(columns) <= row.keys()
                    or row["id"] in identities
                ):
                    raise HubError("invalid query row", category="pagination_receipt")
                identities.add(row["id"])
                rows.append(row)
            cursor = page["next_cursor"]
            if cursor is None:
                return rows
            if not isinstance(cursor, str) or not cursor or cursor in cursors or not page["rows"]:
                raise HubError("invalid query cursor", category="pagination_cursor")
            cursors.add(cursor)
            body["cursor"] = cursor

    def _read_page(self, body):
        return self.read("/v1/rows/pull", body)

    def read(self, route, body):
        """An idempotent read POST (rows/pull, cursor), retried on transient failures."""
        for delay in (*self.read_retries, None):
            try:
                return self._post(route, body)
            except HubError as exc:
                status = exc.diagnostic.get("status")
                transient = exc.diagnostic["category"] == "transport" or (status or 0) >= 500
                if delay is None or not transient or isinstance(exc, RevisionConflict):
                    raise
                time.sleep(delay)

    def insert(self, table, row):
        row = {"updated_at": _stamp(), **row}  # the hub requires the writer's clock
        receipt = self._post(
            "/v1/rows/insert", {"table": table, "columns": sorted(row), "rows": [row]}
        )
        if (
            not all(
                isinstance(receipt.get(key), list) for key in ("inserted", "existing", "rejected")
            )
            or receipt["rejected"]
            or receipt["inserted"] + receipt["existing"] != [row["id"]]
        ):
            raise HubError("invalid or rejected insert receipt")
        return receipt

    def insert_rows(self, table, rows):
        if not rows:
            return {"inserted": [], "existing": [], "rejected": []}
        if len(rows) > 100:
            raise HubError("insert batch exceeds 100 rows")
        ids = [row["id"] for row in rows]
        if len(set(ids)) != len(ids) or any(set(row) != set(rows[0]) for row in rows):
            raise HubError("insert rows require unique IDs and identical columns")
        receipt = self._post(
            "/v1/rows/insert", {"table": table, "columns": sorted(rows[0]), "rows": rows}
        )
        if not all(
            isinstance(receipt.get(key), list) for key in ("inserted", "existing", "rejected")
        ):
            raise HubError("invalid insert receipt")
        accounted = receipt["inserted"] + receipt["existing"]
        if receipt["rejected"] or len(accounted) != len(ids) or set(accounted) != set(ids):
            raise HubError("incomplete or rejected insert receipt")
        return receipt

    def patch(self, table, row_id, values, revision):
        receipt = self._post(
            "/v1/rows/patch",
            {"table": table, "id": row_id, "values": values, "expected_revision": revision},
        )
        clocks = receipt.get("revision")
        if (
            receipt.get("id") != row_id
            or not isinstance(clocks, dict)
            or not isinstance(clocks.get("updated_at"), str)
            or "hub_at" not in clocks
        ):
            raise HubError("invalid patch receipt")
        return receipt

    def catalog(self) -> dict:
        try:
            r = self._http.get(f"{self.base}/v1/catalog", headers=self._headers)
        except httpx.HTTPError as e:
            raise HubError(f"hub unreachable: {type(e).__name__}", route="/v1/catalog") from e
        if r.status_code >= 400:
            raise HubError(f"hub HTTP {r.status_code}: {r.text[:300]}", route="/v1/catalog")
        return r.json()

    def get_file(self, key: str) -> bytes | None:
        """A retained file's bytes, or None when the hub has no such object."""
        return self._file("GET", key)

    def put_file(self, key: str, data: bytes, content_type: str) -> None:
        self._file("PUT", key, data, content_type)

    def _file(self, method, key, data=None, content_type=None):
        headers = {**self._headers, **({"Content-Type": content_type} if content_type else {})}
        try:
            r = self._http.request(
                method,
                f"{self.base}/v1/files/{quote(key, safe='/')}",
                headers=headers,
                content=data,
            )
        except httpx.HTTPError as e:
            raise HubError(
                f"hub unreachable: {type(e).__name__}", category="transport", route="/v1/files"
            ) from e
        if method == "GET" and r.status_code == 404:
            return None
        if r.status_code >= 400:
            raise HubError(
                f"hub HTTP {r.status_code}: {r.text[:300]}",
                category="http",
                status=r.status_code,
                route="/v1/files",
            )
        return r.content

    def pull_page(self, table: str, columns: list[str], after=None) -> dict:
        body = {"table": table, "columns": columns, "since": "", "limit": 200}
        if after is not None:
            body["after"] = after
        return self._post("/v1/rows/pull", body)

    def push(self, table: str, rows: list[dict]) -> dict:
        if not rows:
            return {"upserted": 0, "rejected": []}
        stamp = _stamp()
        groups = defaultdict(list)
        for row in rows:
            row = {"updated_at": stamp, **row}
            # Spotify and saved recovery batches may carry seconds or UTC offsets.
            for col in ("added_at", "liked_at"):
                if row.get(col) is None:
                    continue
                try:
                    value = datetime.fromisoformat(row[col])
                    if value.tzinfo is None:
                        raise ValueError("timezone required")
                except (TypeError, ValueError) as exc:
                    raise HubError(f"{col}: expected a timezone-aware ISO-8601 timestamp") from exc
                row[col] = (
                    value.astimezone(timezone.utc)
                    .isoformat(timespec="milliseconds")
                    .replace("+00:00", "Z")
                )
            groups[tuple(sorted(row))].append(row)
        total, rejected = 0, []
        for keys, group in groups.items():
            for i in range(0, len(group), PUSH_CHUNK):
                out = self._post(
                    "/v1/rows/push",
                    {"table": table, "columns": list(keys), "rows": group[i : i + PUSH_CHUNK]},
                )
                total += out["upserted"]
                rejected += out.get("rejected", [])
                if rejected:
                    raise HubError(f"{table}: {len(rejected)} rejected, first: {rejected[0]}")
        if rejected:
            raise HubError(f"{table}: {len(rejected)} rejected, first: {rejected[0]}")
        return {"upserted": total, "rejected": []}

    def derive(self, table: str, ids: list[str], col: str | None = None) -> dict:
        derived, failed = 0, []
        for i in range(0, len(ids), DERIVE_CHUNK):
            body = {"table": table, "ids": ids[i : i + DERIVE_CHUNK]}
            if col is not None:
                body["col"] = col
            out = self._post("/v1/derive", body)
            derived += out.get("derived", 0)
            failed += out.get("failed", [])
        return {"derived": derived, "failed": failed}
