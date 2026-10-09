# AGENTS.md

music-sync - keeps a personal music catalog in soma and materializes
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
- `flags_task_config` optionally selects Soma flag tasks. Runtime title,
  project/default values, columns and open statuses stay in the workspace.
  `soma_flags.py` retains per-workspace notification batches in the existing
  project-owned R2 recovery bucket under `music-sync/flag-tasks/`. Conditional
  notes appends merge only after definitive conflicts; unknown write outcomes
  require retained marker evidence. Never discard an ambiguous batch or replace
  completed/deleted target identities. The serialized worker owns all recovery.
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
  capture drain rechecks active status before delivering, so a revoke blocks
  queued work (`status: rejected`).
- `just clients issue "<device>"` (scripts/capture_clients.py, operator Modal
  auth) issues a client and prints an enrollment link to the static
  `capture-enroll` page, which hands the fragment-borne URL and token to
  `offlineshazam://enroll`. Onboarding another device or person = that link.
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
  The one exception is consumer capture (accept-and-queue, like Synapse's
  receiver): the `capture-consumer` ASGI app validates auth and payload, stores
  the capture in its receipt plus an empty marker under `music-sync/capture-queue/`
  and answers 202 without Spotify or hub I/O; `GET /<capture_id>` reports its
  status. `capture_drain` (its own function, one container, one input; started
  after each accepted capture, by status reads that find one due and hourly by
  the reconcile cron) delivers markers oldest first through the old synchronous
  `deliver` step, outside the worker. It reads the catalog fresh and never writes
  the worker's mirror cache (a concurrent writer could leave its cursor ahead of
  its rows), waits while pending intent exists, gates the whole queue on Spotify's
  429 Retry-After (`music-sync/capture-spotify-gate.json.gz`), rechecks a
  `no_match` daily and backs other failures off from 1 minute to 1 hour (honoring
  any Retry-After). A capture can land while a reconcile is still reading; the
  next reconcile observes that membership.
- `writes=False` selects observation-only planning before enforcement. Import
  actual liked values and full memberships; never persist hypothetical FIFO,
  auto-like, rule or expiry changes during review. Pending curation intents
  survive observation imports in the owning app recovery store.
- Every Spotify mutation flow archives its pre-write pull, including capture
  and resumed reconciliation. Raw pulls belong to Soma and are uploaded
  through `/v1/files/` under `raw/spotify-pull/` and `raw/spotify-capture/`
  using the scoped hub token. This is an
  approved shared-service contract; no Soma R2 credentials reach this app.
  Recovery state belongs to this project's `music-sync-state` R2 bucket.
  Capture-client hashes live at `music-sync/capture-clients.json.gz`; durable
  delivery state lives below `music-sync/capture-receipts/`, keyed by client
  and capture UUID. A selected Spotify track with a valid ISRC, track ID and
  URI is stored there before side effects and replaced by the success receipt
  only after completion. Incomplete selections remain unstored and retryable.
  Both use the supported `core.archive` R2 abstraction.
  Consumer responses bind `spotify_outcome` to `capture_id`: `not_added` means
  this capture never crossed its Spotify-attempt marker; `unknown` means an
  attempt may have taken effect; `added` means an acknowledged add or observed
  inbox membership. Persist `unknown` before Spotify and `added` immediately
  after acknowledgement. Retain known outcomes through catalog failures;
  legacy incomplete receipts are unknown (and queue as such on their next
  POST). HTTP errors alone never prove an add failed. A repeated POST of the
  same capture answers its current state: 202 while queued (Retry-After when a
  retry time is known), the delivered receipt (200, `ok`, `isrc`, `spotify_outcome:
  added`) once added, 422 `no_match` with Retry-After until the daily recheck;
  confirmed adds remain silent even when catalog maintenance
  needs retry. Never log exception locals containing settings or credentials.
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
  nulls. Preserve membership identity and original timestamps as evidence.
- Curated membership is independent of likes; no unlike removal or re-like
  restoration. Full observations feed per-workspace curation state under
  `music-sync/curation/`; commit its baseline only after all apply batches finish.
  First observation never executes the initial like migration. New curation
  auto-like intent persists through imports; unlike-while-curated exceptions
  persist and block later re-likes. Each exception is a `review_item`; with a
  Soma flag binding it is delivered once as its own create-only task row
  (`soma_flags.deliver_reviews`, stable ID, no notification). Unknown playlists
  need classification; duplicate/alias choices need review. Never automatically
  select a duplicate keeper outside an owner-confirmed rollout package.
  Duplicate inbox recordings also hold FIFO trimming and membership replacement;
  capture still retains recognition history and confirms existing presence.
  Playlist-rule references use `in_playlist_ids_any` / `not_in_playlist_ids`;
  a shared predicate lives in one smart rule and others reuse it with
  `matches_rule_ids_any` / `not_matches_rule_ids` (cycles, dangling and
  non-smart references fail for review);
  legacy name references remain readable but ambiguous names fail for review.
- Checkpoint like attempts before sending. A recovered uncertain attempt needs
  positive live evidence; absent likes require review, never automatic re-like.
  Mutating recovery intent without the current policy version is held.
- `Hub.push` adds one UTC millisecond `updated_at` per invocation where absent,
  preserving supplied timestamps and caller rows. Spotify `added_at` and
  `liked_at` are normalized to UTC milliseconds on the wire, including replay.
  JSON objects and arrays pass through to the hub unchanged.
- Reconciliation stamps missing `updated_at` values on copied hub operation
  rows before checkpointing or mutating Spotify/the hub. Pending retries reuse
  those stamps; unstamped pending rows are checkpointed with a stamp before
  submission. Supplied timestamps, including null for hub rejection, and caller
  payloads stay unchanged. Dry runs do not prepare or save timestamps.
- `scripts/preview.py --output <private-path>` is the read-only owning rollout
  interface. It retains full inputs, revisions and exact migration candidates in
  a new local mode-0600 receipt; it cannot apply the migration. Hub reads exhaust
  keyset pages and reject incomplete/repeated cursors. Collection is not atomic.
  Spotify collection requires complete pages and stable totals, rejects repeated
  or foreign continuation URLs, and checks returned offsets. Any failure stops
  before baseline advancement or mutations; a missing page is never an unlike.
  A missing/changed recording identity on a known liked Spotify alias also stops
  collection for review. `scripts/followed_artists.py --output <private-path>`
  exports the complete followed-artist observation with the existing grant;
  no artist import, follow/unfollow, credential change or inferred follow date.
- Changed raw playlist contents produce occurrence-specific `insert_edge`
  provenance through create-only rows/insert, in bounded 100-row batches. Require
  every ID in the receipt; retain pending intent on partial/invalid receipts.
  Evidence detail stores the original JSON Pointer, not copied event JSON or
  inferred dates. Raw archive references remain the source of original history.
  Capture raw archives retain accepted request fields and server receipt time;
  never label receipt time as recognition time.
  Recognition events use a workspace/client/capture-ID identity, a retained
  original under `raw/spotify-capture/events/`, and create-only provenance with
  the exact `/event` locator. Retries retain the first original and row timestamp;
  changed payloads cannot reuse that identity. Optional `recognized_at` must be
  timezone-aware; omission means unknown. Legacy requests without a capture ID
  cannot distinguish retries from distinct recognitions.
- Conflicting Spotify display profiles remain for review and do not replace
  existing metadata. Multiple current aliases cannot choose an auto-like target.
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
- **soma is written ONLY by this app.** Agents and the user write
  Spotify directly (the `spotify_player` CLI via the `spotify` skill, or the
  Spotify app itself) - never soma. The hourly reconcile is what mirrors
  those Spotify changes into the catalog. Playlist kind/rule/pinned/expiry
  changes go through `just rules` (`core/smart.py` in the worker); there is no
  agent write exception for those columns.
- Smart rules reference playlists by stable ID and are validated against the
  current catalog before the worker writes the row; the hub's
  `playlists-rule-iff-smart` invariant is the only installed catalog rule for
  them. No static rule snapshot is installed in the catalog.
- The one-time rollout package (`core/package.py`, `scripts/rollout.py`) is
  built from a preview receipt, the owner's decision manifest and a private spec
  kept outside git. Apply needs `confirm` = the package digest, validates every
  precondition against a fresh complete pull before the first write, checkpoints
  under the pending key with `intent=package`, and verifies by readback.
  Spotify deletes playlist items by URI only: a repeated URI is deleted and the
  kept occurrence re-inserted at its final index; every intermediate state is
  precomputed so a resumed run knows where it stopped. Renamed originals are
  untouched rollback copies. Package likes join the curation baseline as the
  app's own; its normalization unlikes leave the liked baseline without
  creating review exceptions, however they were executed. Every Spotify write
  is checkpointed (renames, creations, edit ops, like/unlike chunks); a rate
  limit longer than the client waits stops the run with the exact `remaining`
  actions and keeps the checkpoint, so another client can finish them and a
  rerun of the same package adopts that work without repeating it.
- Recognition events (captures and imported history) each have a retained
  original and one create-only provenance row. The songs Shazam summary
  (`shazamed`, `shazam_count`, `shazam_first_at`, `shazam_last_at`,
  `shazam_dates_estimated`) is recomputed from those originals, so a retried
  capture never counts twice. Exact times come only from `recognized_at`;
  playlist add dates and receipt times are estimates.
- The worker loads the mirror incrementally: each slice's rows and `hub_at`
  cursor live under `music-sync/mirror-cache/` in the recovery bucket, deltas use
  the hub's inclusive `since`, the cache is checkpointed after each slice and
  rebuilt weekly. Dry runs never write it. Worker calls get 3,600 s; apply stops
  cleanly 3,000 s into a call and stays pending. Worker hub clients retry
  transient page-read failures twice.
- Observation imports (`writes=False`) run in the worker (`observe`), return
  their flags instead of filing them and are allowed while reconciliation is
  disabled. Membership rows keep the earliest `added_at` when a replacement
  alias changes the track ID.
- Metadata backfill uses the hub's derivation API and reuses provenance.
  Only Deezer, MusicBrainz and first_year are enrichment targets; album_year
  remains an observed input. Completion requires actual current rows and source
  proofs, not derived counts. Empty/partial results remain unresolved.
  Confirmation merges incremental pulls by ID with independent inclusive
  server `hub_at` cursors. Read proofs before songs and commit both caches and
  cursors only after both reads succeed; evict deleted rows and nonqualifying
  proofs. Any missing stamp keeps that table on full refreshes for the run.
  The operator command supports bounded first_year repair with `--ids-file`
  (JSON array), `--refresh` and `--dry-run`. Refresh requires explicit IDs,
  verifies sole output binding and canonical year consistency, and rejects
  changed inputs/likes or unexpected output without blind retries. Normal
  `complete()` reuse remains unchanged. Expected years follow Derivations:
  int-coercible inputs within 1900-2100 inclusive, minimum accepted value.
  No accepted year means omitted output, never an instruction to clear a value.
  Large refresh previews use 200-row
  song pages filtered to explicit IDs; per-batch reads remain exact-ID scoped.
  Missing/deleted selections abort before writes. Receipts report
  selected IDs and per-row outcomes and stay outside source control. Bounded
  apply flushes partial batch receipts before proceeding, preserving confirmed
  IDs, structured failures and cooldowns if a later batch is interrupted.
  Structured rate limits defer the source immediately; `retry_at` in the
  summary is the earliest Unix timestamp for resuming it. Other sources
  continue, and failed/deferred work never counts as complete.
- `metadata.require_observed_contract` gates fresh and pending mutations,
  capture, replay apply and backfill. All seven Spotify base properties must
  exist without derivation bindings. Dry previews remain available.
  Live cutover is required; this code does not establish remote catalog state.
  Follow `docs/observed-metadata-rollout.md` with deployment/capture downtime
  approval, preserve historical provenance options and leave cron disabled.

## Workspaces (multi-user ready, no user database)

- `default` is the operator's own workspace, configured entirely by env
  (`Settings()`), exactly as before. Any other workspace is one entry in the
  `music-sync/workspaces.json.gz` R2 registry (`core/workspaces.py`) that
  overrides the per-user Settings fields (Spotify refresh token, market,
  soma hub, Notion flags target, limits) via
  `workspaces.settings_for(base, id)`; the Spotify developer app and R2 bucket
  are shared app infrastructure. `Settings.workspace` names the active one.
- Workspace secrets (`spotify_refresh_token`, `soma_hub_token`,
  `notion_token`) are Fernet-encrypted at rest with `WORKSPACE_SECRET_KEY`
  (ENV item, Modal secret `music-sync`); other fields stay plain. Saving a
  secret without the key is refused, and any save rewrites the whole registry,
  re-encrypting a legacy plaintext value. Losing or rotating the key means
  re-entering every workspace's secrets.
- Capture clients carry their workspace; the consumer capture runs entirely
  in it (resolve, capture, hub, flags). Pending-reconcile intent is per
  workspace (`archive.pending_key`; default keeps the original key).
- Reconcile: `RECONCILE_ENABLED=1` stays the app-wide switch; the cron runs
  every workspace (`{id: result}`), and a non-default one also needs its own
  `reconcile_enabled` flag. Manual reconcile takes an optional `workspace`.
- **Connect Spotify** (`core/spotify_connect.py`, the `spotify-connect`
  endpoint): `just workspace connect-link <id>` issues a single-use, 7-day
  invite; the person signs in with Spotify and the refresh token lands on
  their workspace. The endpoint's own URL with a trailing slash is the
  redirect URI registered on the Spotify app; Development Mode needs the
  person's Spotify email on the app's user allowlist. No developer script or
  credential is part of a user's approval.
- Onboarding a person: `just workspace set <id>` (their hub + Notion on
  stdin), send `just workspace connect-link <id>`, then
  `just clients issue "<device>" <id>` for each Cochlea device.

## Layout

```
app.py                       Modal shim: serialized worker, cron and proxy-auth endpoints
src/core/
  spotify_client.py          Spotify Web API client (post-2026-02 Development Mode endpoint set)
  hub.py                     soma hub HTTP client (pull, push, derive)
  mirror.py                  load the mirror (songs, playlists, playlist_songs) from the hub
  metadata.py                pure observed base-field patches and per-field archive evidence
  metadata_replay.py         metadata-only recovery from one retained Spotify archive
  rules.py                   rule JSON validation, -> SQL, -> description
  reconcile.py                pure diff: (mirror, live) -> list of actions
  actions.py                 apply actions to Spotify and the hub; run log
  flags.py                   batch flags into one Notion Chore task (idempotent batch marker)
  soma_flags.py              Soma flag batches and quiet review rows
  capture.py                  /capture: resolve a Shazam result, add to inbox, record the edge
  curation.py                 liked/curated transitions, auto-like intent, unlike exceptions
  history.py                  occurrence evidence for changed playlist contents
  recognition.py              recognition events and the songs Shazam summary
  smart.py                    supported playlist kind/rule configuration
  package.py                  one-time rollout package: build, simulate, apply, verify
  workspaces.py               per-person Settings overrides in the R2 registry
  spotify_connect.py          Connect Spotify invites + OAuth code exchange
  capture_clients.py          capture-only credentials and idempotent delivery receipts
  config.py                  Settings (env vars only)
  model.py                    dataclasses shared across core
  archive.py                  raw files via soma, recovery via project-owned R2
scripts/
  provision.py                R2 field minters + atomic, memory-only Modal token batch
  sync_secrets.py             push .env.tpl -> Modal secret store
  spotify_auth.py             mint/re-mint the default workspace's Spotify refresh token (local loopback)
  workspace.py                operator CLI for workspaces (`just workspace ...`)
  rules.py                    playlist kind/rule configuration (`just rules ...`)
  preview.py                  read-only dry-run receipt (local or `--remote`, optional `--package`)
  rollout.py                  observe / package build+apply / recognition import (receipts stay private)
  capture_clients.py          capture client issue/revoke (`just clients ...`)
  backfill_derive.py          enrichment backfill through the hub derivation API
  followed_artists.py         read-only followed-artist receipt
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
| `just deploy` | test + sync-secrets + `modal deploy` (CI deploys on push to main) |
| `just rules <action>` | Playlist kinds and smart rules through the worker |
| `just clients` / `just workspace` | Capture clients / workspace records |

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
