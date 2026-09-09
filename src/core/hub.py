"""life-data hub client over the HTTP protocol. Knows a URL and a bearer token, nothing else."""

from collections import defaultdict

import httpx

PUSH_CHUNK = 500
DERIVE_CHUNK = 50
USER_AGENT = "music-sync/0.1 (+https://github.com/alexjmiller5/music-sync)"


class HubError(RuntimeError):
    pass


class Hub:
    def __init__(self, base_url: str, token: str, http: httpx.Client | None = None):
        self.base = base_url.rstrip("/")
        self._http = http or httpx.Client(timeout=120)
        self._headers = {"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT}

    def _post(self, route: str, body: dict) -> dict:
        try:
            r = self._http.post(f"{self.base}{route}", json=body, headers=self._headers)
        except httpx.HTTPError as e:
            raise HubError(f"hub unreachable: {type(e).__name__}") from e
        if r.status_code >= 400:
            raise HubError(f"hub HTTP {r.status_code}: {r.text[:300]}")
        return r.json()

    def pull(self, table: str, columns: list[str], since: str = "") -> list[dict]:
        return self._post("/v1/rows/pull", {"table": table, "columns": columns, "since": since})[
            "rows"
        ]

    def push(self, table: str, rows: list[dict]) -> dict:
        if not rows:
            return {"upserted": 0, "rejected": []}
        groups = defaultdict(list)
        for row in rows:
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

    def derive(self, table: str, ids: list[str]) -> dict:
        derived, failed = 0, []
        for i in range(0, len(ids), DERIVE_CHUNK):
            out = self._post("/v1/derive", {"table": table, "ids": ids[i : i + DERIVE_CHUNK]})
            derived += out.get("derived", 0)
            failed += out.get("failed", [])
        return {"derived": derived, "failed": failed}
