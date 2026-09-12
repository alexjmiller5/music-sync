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
- Notion uses the dedicated Music Sync connection stored in this project's ENV
  item. Its read, insert and update capabilities cover the Tasks database and
  this project's page for flag tasks and their project relation. No user
  information, comments or agent access is enabled. Notion applies capabilities
  across all granted content; the app updates only its own flag tasks. Never
  substitute the agent's integration or grant the entire Projects database.
- Reconcile, legacy capture and capture-client issue/revoke use Modal proxy
  auth (`Modal-Key` + `Modal-Secret`). The consumer capture endpoint is public
  at the Modal edge and requires an app-issued capture-only Bearer token. Only
  its SHA-256 hash is retained; each client can be revoked independently. The
  worker rechecks active status after queueing so a completed revoke blocks
  later queued work.
- Cron: Modal is the PREFERRED home for schedules - but the Starter plan
  allows **5 deployed crons across ALL apps**, so track the budget. Overflow
  goes to GHA cron or CF Cron Triggers.
- **Cron and mutating manual reconciliation require `RECONCILE_ENABLED=1`.**
  The manifest carries the field; provisioning initializes it to `0`. Explicit
  dry runs are allowed while disabled; capture is independently authorized.
- Every mutating endpoint synchronously calls one `worker.remote(...)` with
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
  Capture-client hashes live at `music-sync/capture-clients.json.gz`; durable
  delivery state lives below `music-sync/capture-receipts/`, keyed by client
  and capture UUID. A selected Spotify track with a valid ISRC, track ID and
  URI is stored there before side effects and replaced by the success receipt
  only after completion. Incomplete selections remain unstored and retryable.
  Both use the supported `core.archive` R2 abstraction.
  R2 pending intent lives at
  `music-sync/pending-reconcile.json.gz`, outside raw-backup lifecycle rules.
  Archive credentials require object read and write. A failure stops remaining
  operations and preserves the pending batches; resume before taking a new
  baseline. Do not remove retry evidence manually. Capture cannot overtake a
  pending reconcile. An observation import may resume without enabling writes.
- Metadata-only replay uses the same pending key with `intent=metadata_replay`.
  Reconcile and capture check pending intent before Spotify setup; replay
  resumes only its own archive key and observation time, through the serialized
  worker. It defaults to dry-run and explicit boolean false permits apply
  without enabling enforcement. It fills existing songs only and checkpoints
  remaining provenance with song patches. Retained source market is used only
  when explicitly present in the archive; otherwise evidence market is null.
- Consumer capture accepts five required string fields plus an optional ISRC.
  A supplied ISRC is normalized, searched directly and must
  match the returned recording. Without one, capture requires normalized exact
  title and artist identity; it never strips version suffixes or accepts an
  artist-free title match.
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
- Observed Spotify base fields use `metadata.observation_actions` in reconcile,
  observation import and capture. Display fields retain per-field archive
  evidence (`takeout` / `evidence_of`, timestamp in `detail.observed_at`).
  Album and year form one release pair; an initial partial pair is retained
  without combining it with a later partial observation.
  Playability evidence is per track and market. Missing availability is unknown;
  legacy alias lists alone never authorize a replacement. Capture archives the
  full `resolved_track` with its pre-write inbox items.
- **life-data is written ONLY by this app.** Agents and the user write
  Spotify directly (the `spotify_player` CLI via the `spotify` skill, or the
  Spotify app itself) - never life-data. The hourly reconcile is what mirrors
  those Spotify changes into the catalog.
- Metadata backfill uses the hub's derivation API and reuses provenance.
  Only Deezer, MusicBrainz and first_year are enrichment targets; album_year
  remains an observed input. Completion requires actual current rows and source
  proofs, not derived counts. Empty/partial results remain unresolved.
  Confirmation merges incremental pulls by ID with independent inclusive
  server `hub_at` cursors. Read proofs before songs and commit both caches and
  cursors only after both reads succeed; evict deleted rows and nonqualifying
  proofs. Any missing stamp keeps that table on full refreshes for the run.
  Structured rate limits defer the source immediately; `retry_at` in the
  summary is the earliest Unix timestamp for resuming it. Other sources
  continue, and failed/deferred work never counts as complete.
- `metadata.require_observed_contract` gates fresh and pending mutations,
  capture, replay apply and backfill. All seven Spotify base properties must
  exist without derivation bindings. Dry previews remain available.
  Live cutover is required; this code does not establish remote catalog state.
  Follow `docs/observed-metadata-rollout.md` with deployment/capture downtime
  approval, preserve historical provenance options and leave cron disabled.

## Layout

```
app.py                       Modal shim: serialized worker, cron and proxy-auth endpoints
src/core/
  spotify_client.py          Spotify Web API client (post-2026-02 Development Mode endpoint set)
  hub.py                     life-data hub HTTP client (pull, push, derive)
  mirror.py                  load the mirror (songs, playlists, playlist_songs) from the hub
  metadata.py                pure observed base-field patches and per-field archive evidence
  metadata_replay.py         metadata-only recovery from one retained Spotify archive
  rules.py                   rule JSON validation, -> SQL, -> description
  reconcile.py                pure diff: (mirror, live) -> list of actions
  actions.py                 apply actions to Spotify and the hub; run log
  flags.py                   batch flags into one Notion Chore task
  capture.py                  /capture: resolve a Shazam result, add to inbox, record the edge
  capture_clients.py          capture-only credentials and idempotent delivery receipts
  config.py                  Settings (env vars only)
  model.py                    dataclasses shared across core
  archive.py                  raw files via life-data, recovery via project-owned R2
scripts/
  provision.py                R2 field minters + atomic, memory-only Modal token batch
  sync_secrets.py             push .env.tpl -> Modal secret store
  spotify_auth.py             mint/re-mint the Spotify refresh token
  create_50s_playlist.py      one-off playlist creation
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

## Credential provisioning

Music Sync owns its Modal app, runtime Secret and independently minted CI
token in its own vault. Modal Starter personal tokens retain workspace-level
permissions; separate tokens allow independent rotation but do not restrict
access to one app. Environment-scoped service users require Team or Enterprise.

`op-project-bootstrap` calls `scripts/provision.py --batch modal-token` once
for the Modal CI pair and saves both fields through JSON stdin. The operator
opens the stderr approval URL in the configured remote browser session and
approves the displayed code. No local browser opens or token cache is written.
R2 provisioning retains its `--field` interface and manifest order.
