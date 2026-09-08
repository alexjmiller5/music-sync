set shell := ["bash", "-cu"]

default:
    @just --list

# Dev: live-reloading deploy of app.py against real Modal infra
dev:
    uv run modal serve app.py

test:
    uv run pytest

# All static analysis (read-only, CI-safe)
check:
    uv run ruff check . && uv run ruff format --check .

fmt:
    uv run ruff format . && uv run ruff check --fix .

# Stream logs from the deployed app
logs:
    uv run modal app logs music-sync

# Push .env.tpl secrets into the Modal secret store (no plaintext touches disk;
# the modal CLI rejects process-substitution FIFOs, hence the stdin script)
sync-secrets:
    op inject -i .env.tpl | uv run scripts/sync_secrets.py music-sync

deploy: test sync-secrets
    uv run modal deploy app.py

# --- project-specific recipes below (one-offs live in scripts/, run directly) ---
