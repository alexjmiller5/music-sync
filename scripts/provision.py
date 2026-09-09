# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx"]
# ///
"""Mint this project's machine-creatable credentials (op-project-bootstrap
provision contract: --list prints mintable field names; --field NAME prints
ONLY the value to stdout, progress on stderr).

R2_API_TOKEN: a Cloudflare API token scoped to object reads and writes on ONE bucket
(the project-owned recovery bucket). Existing credentials are never revoked
by provisioning; rotation must verify the replacement before revocation. Needs the AI Agent CF token (User API Tokens: Edit).
R2_ACCESS_KEY_ID: resolves the named token's ID after R2_API_TOKEN is minted.
The existing token value stays in the environment item; no R2 cache is written.

token-id/token-secret: a CI-only Modal token for this project. Modal has no
token-minting API - `modal token new` is a browser flow - so this minter
opens a browser tab ONCE per project and Alex approves it. Bootstrap already
runs in his desktop-authenticated terminal, so that is fine, and the result
is a CI token dedicated to this project: revoking it kills this repo's
deploys and nothing else.

The mint writes to a private temp config (MODAL_CONFIG_PATH) rather than
~/.modal.toml, so no credential lands in the real config; the temp file is
0600 and removed as soon as the second field is read. One browser flow serves
both fields - the second `--field` call reads the file the first one wrote.
"""

import os
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

import httpx

BUCKET = "music-sync-state"
NAME = "music-sync-state-r2"
OP_CF_TOKEN = "op://4eeyrkqibibn7k4j6rz2fbzvxm/mxxpo6neiz3grdyrjj7rv7nume/credential"

PROJECT = "music-sync"  # also the Modal profile name for this project's CI token
MODAL_FIELDS = {"token-id": "token_id", "token-secret": "token_secret"}
CACHE = Path(tempfile.gettempdir()) / f"modal-ci-{PROJECT}.toml"

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


def modal_bin() -> list[str]:
    """The project's pinned modal, bypassing Alex's PATH wrapper (which would
    inject his personal token and make --verify check the wrong credential)."""
    venv = Path(__file__).resolve().parent.parent / ".venv" / "bin" / "modal"
    return [str(venv)] if venv.exists() else ["uvx", "modal"]


def mint_modal_token() -> None:
    log(f"· minting a CI-only Modal token for {PROJECT} (a browser tab will open)")
    env = {k: v for k, v in os.environ.items() if k not in ("MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET")}
    env["MODAL_CONFIG_PATH"] = str(CACHE)
    CACHE.touch(mode=0o600)
    subprocess.run(
        [*modal_bin(), "token", "new", "--profile", f"{PROJECT}-ci", "--no-activate"],
        env=env,
        check=True,
        stdout=sys.stderr,
    )


def read_modal_field(field: str) -> str:
    if not CACHE.exists() or CACHE.stat().st_size == 0:
        mint_modal_token()
    profiles = tomllib.loads(CACHE.read_text())
    profile = profiles.get(f"{PROJECT}-ci") or next(iter(profiles.values()))
    return profile[MODAL_FIELDS[field]]


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
        case ["--field", name] if name in MODAL_FIELDS:
            value = read_modal_field(name)
            # Last field consumed: the temp credential has served its purpose.
            if name == "token-secret":
                CACHE.unlink(missing_ok=True)
            print(value)
        case _:
            sys.exit("usage: provision.py --list | --field <name>")


if __name__ == "__main__":
    main()
