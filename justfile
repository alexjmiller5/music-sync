set shell := ["bash", "-cu"]

default:
    @just --list

# Dev: live-reloading deploy of app.py against real Modal infra
dev:
    modal serve app.py

test:
    uv run pytest

# All static analysis (read-only, CI-safe)
check:
    uv run ruff check . && uv run ruff format --check .

fmt:
    uv run ruff format . && uv run ruff check --fix .

# Stream logs from the deployed app
logs:
    modal app logs music-sync

# Push .env.tpl secrets into the Modal secret store (no plaintext touches disk;
# the modal CLI rejects process-substitution FIFOs, hence the stdin script).
# This one drives the Modal SDK rather than the CLI, so the `modal` PATH wrapper
# can't inject auth for it - op run does it instead.
sync-secrets:
    MODAL_TOKEN_ID=op://4eeyrkqibibn7k4j6rz2fbzvxm/2sfxybjpv3c3ohzxhf5qeken4a/token_id MODAL_TOKEN_SECRET=op://4eeyrkqibibn7k4j6rz2fbzvxm/2sfxybjpv3c3ohzxhf5qeken4a/token_secret op run --no-masking -- bash -c "op inject -i .env.tpl | uv run scripts/sync_secrets.py music-sync"

deploy: test sync-secrets
    modal deploy app.py

# --- project-specific recipes below (one-offs live in scripts/, run directly) ---
