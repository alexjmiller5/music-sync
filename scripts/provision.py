# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx", "modal>=1.0,<2"]
# ///
"""Mint this project's machine-creatable credentials (op-project-bootstrap
provision contract: --list prints mintable field names; --field NAME prints
ONLY the value to stdout, progress on stderr).

R2_API_TOKEN: a Cloudflare API token scoped to object reads and writes on ONE bucket
(the project-owned recovery bucket). Existing credentials are never revoked
by provisioning; rotation must verify the replacement before revocation. Needs the AI Agent CF token (User API Tokens: Edit).
R2_ACCESS_KEY_ID: resolves the named token's ID after R2_API_TOKEN is minted.
The existing token value stays in the environment item; no R2 cache is written.

token-id/token-secret: a CI-only Modal token pair minted together in memory.
The operator approves the stderr URL/code in the configured remote browser;
bootstrap saves the verified JSON pair atomically to the project vault.
"""

import json
import os
import subprocess
import sys

import httpx

BUCKET = "music-sync-state"
NAME = "music-sync-state-r2"
OP_CF_TOKEN = "op://4eeyrkqibibn7k4j6rz2fbzvxm/mxxpo6neiz3grdyrjj7rv7nume/credential"

PROJECT = "music-sync"
MODAL_FIELDS = ("token-id", "token-secret")
MAX_ATTEMPTS = 15

FIELDS = [
    "RECONCILE_ENABLED",
    "R2_API_TOKEN",
    "R2_ACCESS_KEY_ID",
    "R2_ACCOUNT_ID",
    "R2_BUCKET",
    *MODAL_FIELDS,
]


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def op_read(ref: str) -> str:
    return subprocess.run(
        ["op", "read", ref], capture_output=True, text=True, check=True
    ).stdout.strip()


def account_id(client: httpx.Client | None = None) -> str:
    if value := os.environ.get("CLOUDFLARE_ACCOUNT_ID"):
        return value
    if client is None:
        with httpx.Client(
            base_url="https://api.cloudflare.com/client/v4",
            headers={"Authorization": f"Bearer {op_read(OP_CF_TOKEN)}"},
        ) as client:
            return account_id(client)
    accounts = client.get("/accounts").raise_for_status().json()["result"]
    if len(accounts) != 1:
        raise RuntimeError("Set CLOUDFLARE_ACCOUNT_ID to choose the account")
    return accounts[0]["id"]


def mint_r2_token() -> str:
    admin = op_read(OP_CF_TOKEN)
    c = httpx.Client(
        base_url="https://api.cloudflare.com/client/v4",
        headers={"Authorization": f"Bearer {admin}"},
    )
    for t in (
        c.get("/user/tokens", params={"per_page": 100}).raise_for_status().json()["result"] or []
    ):
        if t["name"] == NAME:
            raise RuntimeError(f"{NAME} exists; preserve it until a replacement is verified")
    account = account_id(c)
    bucket_route = f"/accounts/{account}/r2/buckets"
    buckets = c.get(bucket_route).raise_for_status().json()["result"]["buckets"]
    if not any(bucket["name"] == BUCKET for bucket in buckets):
        c.post(bucket_route, json={"name": BUCKET}).raise_for_status()
    groups = c.get("/user/tokens/permission_groups").raise_for_status().json()["result"]
    permissions = [
        g
        for g in groups
        if g["name"]
        in {"Workers R2 Storage Bucket Item Write", "Workers R2 Storage Bucket Item Read"}
    ]
    if len(permissions) != 2:
        raise RuntimeError("R2 object read and write permissions are both required")
    r = c.post(
        "/user/tokens",
        json={
            "name": NAME,
            "policies": [
                {
                    "effect": "allow",
                    "resources": {f"com.cloudflare.edge.r2.bucket.{account}_default_{BUCKET}": "*"},
                    "permission_groups": [{"id": g["id"]} for g in permissions],
                }
            ],
        },
    ).raise_for_status()
    log("✓ R2 bucket-scoped read/write token minted")
    return r.json()["result"]["value"]


def r2_access_key_id() -> str:
    """Look up the minted token without rotating it or caching its secret."""
    ids, page = [], 1
    with httpx.Client(
        base_url="https://api.cloudflare.com/client/v4",
        headers={"Authorization": f"Bearer {op_read(OP_CF_TOKEN)}"},
    ) as client:
        while True:
            body = (
                client.get("/user/tokens", params={"per_page": 100, "page": page})
                .raise_for_status()
                .json()
            )
            ids.extend(t["id"] for t in body["result"] if t["name"] == NAME)
            if page >= body.get("result_info", {}).get("total_pages", page):
                break
            page += 1
    if len(ids) != 1:
        raise RuntimeError(f"Expected exactly one {NAME} token; mint R2_API_TOKEN first")
    return ids[0]


def mint_modal_token() -> dict[str, str]:
    from modal.client import Client
    from modal.config import DEFAULT_SERVER_URL
    from modal.token_flow import TokenFlow

    with Client.anonymous(DEFAULT_SERVER_URL) as client:
        flow = TokenFlow(client)
        with flow.start() as (_, url, code):
            print(
                f"Approve a dedicated {PROJECT} CI token in the remote browser:\n{url}",
                file=sys.stderr,
            )
            print(f"Verification code: {code}", file=sys.stderr)
            for _ in range(MAX_ATTEMPTS):
                result = flow.finish(timeout=40)
                if result is not None:
                    break
            else:
                raise RuntimeError("Modal approval timed out")
    if not result.token_id.strip() or not result.token_secret.strip():
        raise RuntimeError("Modal returned an incomplete token pair")
    Client.verify(DEFAULT_SERVER_URL, (result.token_id, result.token_secret))
    return dict(zip(MODAL_FIELDS, (result.token_id, result.token_secret), strict=True))


def main() -> None:
    match sys.argv[1:]:
        case ["--list"]:
            print("\n".join(FIELDS))
        case ["--field", "RECONCILE_ENABLED"]:
            print("0")
        case ["--field", "R2_API_TOKEN"]:
            print(mint_r2_token())
        case ["--field", "R2_ACCESS_KEY_ID"]:
            print(r2_access_key_id())
        case ["--field", "R2_ACCOUNT_ID"]:
            print(account_id())
        case ["--field", "R2_BUCKET"]:
            print(BUCKET)
        case ["--batches"]:
            print(json.dumps({"modal-token": MODAL_FIELDS}))
        case ["--batch", "modal-token"]:
            try:
                pair = mint_modal_token()
            except Exception:
                sys.exit("Modal token mint or verification failed; no credentials emitted")
            print(json.dumps(pair))
        case _:
            sys.exit(
                "usage: provision.py --list | --field <R2 field> | --batches | --batch modal-token"
            )


if __name__ == "__main__":
    main()
