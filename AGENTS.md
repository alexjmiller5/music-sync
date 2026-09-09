# AGENTS.md

music-sync - keeps a personal music catalog in life-data and materializes
smart playlists in Spotify from rules over that catalog, deployed on Modal.
Seeded from Spotify GDPR export zips in `data/raw/` (gitignored personal
data - NEVER commit anything under `data/`).

## Architecture rule (the one that matters)

**Business logic lives in `src/core/` as plain Python with NO Modal imports.**
Only `app.py` imports `modal` - it is the deployment shim (image, secrets,
cron, endpoints). This keeps the logic portable: the same `core` package
runs in tests, locally, or on any future platform.

- Spotify uses its own Music Sync developer app and OAuth refresh grant. Never
  reuse a terminal client or another service's client credentials. Development
  Mode quota is shared across the owning developer account, even with separate apps.
- Endpoints use `requires_proxy_auth=True` - callers send `Modal-Key` +
  `Modal-Secret` headers (mint tokens in the Modal dashboard → Settings →
  Proxy Auth Tokens). Never expose an unauthenticated endpoint.
- Cron: Modal is the PREFERRED home for schedules - but the Starter plan
  allows **5 deployed crons across ALL apps**, so track the budget. Overflow
  goes to GHA cron or CF Cron Triggers.
- **Cron and mutating manual reconciliation require `RECONCILE_ENABLED=1`.**
  The manifest carries the field; provisioning initializes it to `0`. Explicit
  dry runs are allowed while disabled; capture is independently authorized.
- All three entrypoints synchronously call one `worker.remote(...)` with
  `max_containers=1` and `@modal.concurrent(max_inputs=1)`. Keep the full
  read/archive/plan/apply cycle inside it; endpoint container caps alone do
  not serialize different functions. FastAPI is a production dependency.
- `writes=False` selects observation-only planning before enforcement. Import
  actual liked values and full memberships; never persist hypothetical FIFO,
  auto-like, relink, undo, rule or expiry changes during review.
- Every Spotify mutation flow archives its pre-write pull, including capture
  and resumed reconciliation. Raw pulls belong to Life Data and are uploaded
  through `/v1/files/` under `raw/spotify-pull/` and `raw/spotify-capture/`
  using the scoped hub token. This is an
  approved shared-service contract; no Life Data R2 credentials reach this app.
  Recovery state belongs to this project's `music-sync-state` R2 bucket.
  R2 pending intent lives at
  `music-sync/pending-reconcile.json.gz`, outside raw-backup lifecycle rules.
  Archive credentials require object read and write. A failure stops remaining
  operations and preserves the pending batches; resume before taking a new
  baseline. Do not remove retry evidence manually. Capture cannot overtake a
  pending reconcile. An observation import may resume without enabling writes.
- R2 object reads/writes use boto3's S3 API with bucket-scoped permissions.
  `R2_ACCESS_KEY_ID` is the token ID; the S3 secret is derived in memory as
  SHA-256 of `R2_API_TOKEN`. Only `NoSuchKey` means a missing object; other
  errors stop the flow. Keep the pending key unchanged.
- Hub patches group by exact present keys. Never turn omitted columns into
  nulls. Preserve membership identity and `added_at` through soft deletion
  for seven-day undo. Un-heart/rule removal/expiry override dedupe re-adds.
- `Hub.push` adds one UTC millisecond `updated_at` per invocation where absent,
  preserving supplied timestamps and caller rows. Spotify `added_at` and
  `liked_at` are normalized to UTC milliseconds on the wire, including replay.
  JSON objects and arrays pass through to the hub unchanged.
- Reconciliation stamps missing `updated_at` values on copied hub operation
  rows before checkpointing or mutating Spotify/the hub. Pending retries reuse
  those stamps; unstamped pending rows are checkpointed with a stamp before
  submission. Supplied timestamps, including null for hub rejection, and caller
  payloads stay unchanged. Dry runs do not prepare or save timestamps.
- Dry-run responses include structured `planned` actions with recording and
  playlist identity, reason and proposed changes; `applied` is confirmed work
  only. No Spotify/hub/archive/Notion writes occur during dry runs.
- **life-data is written ONLY by this app.** Agents and the user write
  Spotify directly (the `spotify_player` CLI via the `spotify` skill, or the
  Spotify app itself) - never life-data. The hourly reconcile is what mirrors
  those Spotify changes into the catalog.

## Layout

```
app.py                       Modal shim: serialized worker, cron and proxy-auth endpoints
src/core/
  spotify_client.py          Spotify Web API client (post-2026-02 Development Mode endpoint set)
  hub.py                     life-data hub HTTP client (pull, push, derive)
  mirror.py                  load the mirror (songs, playlists, playlist_songs) from the hub
  rules.py                   rule JSON validation, -> SQL, -> description
  reconcile.py                pure diff: (mirror, live) -> list of actions
  actions.py                 apply actions to Spotify and the hub; run log
  flags.py                   batch flags into one Notion Chore task
  capture.py                  /capture: resolve a Shazam result, add to inbox, record the edge
  config.py                  Settings (env vars only)
  model.py                    dataclasses shared across core
  archive.py                  raw files via life-data, recovery via project-owned R2
scripts/
  provision.py                mints R2/Modal tokens; resolves R2_ACCESS_KEY_ID after R2_API_TOKEN
  sync_secrets.py             push .env.tpl -> Modal secret store
  spotify_auth.py             mint/re-mint the Spotify refresh token
  create_50s_playlist.py      one-off, deleted after migration step 9
tests/                        pytest
```

## Stack

uv · pydantic-settings (env config) · httpx · boto3 (R2 S3) · structlog · pytest · ruff.
Config comes from env vars only: Modal Secret in the cloud, `op run` locally.
`.env.tpl` is the canonical secrets manifest (op:// refs, committed).
Instantiate `Settings()` inside functions, never at import time.

## Commands

Standard verb set (see global AGENTS.md) - the justfile is the interface,
not a script catalog; one-offs go in `scripts/` and run directly.

| Command | Purpose |
|---|---|
| `just dev` | Live-reload dev against real Modal infra (`modal serve`) |
| `just test` / `just check` / `just fmt` | pytest / ruff read-only / ruff fix |
| `just logs` | Stream deployed-app logs |
| `just sync-secrets` | Push `.env.tpl` → Modal secret store |
| `just deploy` | test + sync-secrets + `modal deploy` |

## TDD

Write the test in `tests/` first, then the `src/core/` code. `app.py` shim
functions stay thin enough to not need tests beyond `tests/test_app.py`,
which covers dispatch, the activation gate and real FastAPI error responses.
Release regressions use dummy state and mocked HTTP; the suite blocks external
sockets while permitting the OAuth callback tests on loopback.
