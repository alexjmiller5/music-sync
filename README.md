# music-sync

Keeps a personal music catalog in life-data and materializes smart playlists
in Spotify from rules over that catalog, deployed on
[Modal](https://modal.com) from the `modal-service` template. Full design:
`docs/superpowers/specs/2026-09-08-music-sync-redesign-design.md`.

## Layout

```
app.py            Modal shim - one serialized worker, hourly trigger, /reconcile and /capture
src/core/         business logic (plain Python, portable, no Modal imports)
  spotify_client.py  Spotify Web API client (post-2026-02 Development Mode endpoint set)
  hub.py              life-data hub HTTP client (pull, push, derive)
  mirror.py           load the mirror (songs, playlists, playlist_songs) from the hub
  rules.py            rule JSON validation, -> SQL, -> description
  reconcile.py         pure diff: (mirror, live) -> list of actions
  actions.py          apply actions to Spotify and the hub; run log
  flags.py            batch flags into one Notion Chore task
  capture.py           /capture: resolve a Shazam result, add to inbox, record the edge
  config.py           Settings (env vars only)
  model.py             dataclasses shared across core
  archive.py           archive a raw pull to R2
scripts/
  provision.py       mints R2_API_TOKEN and the Modal CI token (op-project-bootstrap contract)
  sync_secrets.py     push .env.tpl -> Modal secret store
  spotify_auth.py     mint/re-mint the Spotify refresh token
  create_50s_playlist.py  one-off, deleted after migration step 9
tests/            pytest
data/raw/         Spotify GDPR export zips (gitignored - personal data, NEVER commit)
.env.tpl          secrets manifest (1Password op:// refs, committed)
justfile          dev / test / sync-secrets / deploy
```

## What it does

Every song lives in life-data with `liked` = whether it's hearted in Spotify.
Playlists come in three kinds:

- **inbox** - exactly one, `new songs`, capped at 100. Songs land here from
  the user adding directly or from the Shazam shortcut; membership implies
  nothing (not liked by being there, not removed by un-hearting). Songs that
  age out unliked stay in the catalog with their capture history.
- **curated** - every other playlist the user edits by hand. The reconciler
  never adds or removes songs here except as a consequence of hearting
  (below) or relinking an unplayable track.
- **smart** - membership = a rule over the pool (every liked song), fully
  materialized each run; the description is rewritten each run.

The heart in the Spotify app is the only gesture needed to sort a song into
every smart playlist it matches: liking a song puts it in the pool, adding an
unliked song to a curated playlist likes it, un-hearting removes it from
every curated and smart playlist (soft-deleted, restorable within 7 days by
re-liking). The hourly cron does the diffing; see spec section 5 for the
full event table.

## Endpoints

Both require Modal proxy auth (`Modal-Key` / `Modal-Secret` headers, minted
in the Modal dashboard under Settings -> Proxy Auth Tokens).

**`POST /reconcile`** - runs the reconciler on demand (same logic as the
hourly cron). Body:

```json
{"dry_run": false}
```

Response:

```json
{"summary": "...", "planned": [], "applied": {}, "flags": [], "errors": []}
```

**`POST /capture`** - used by the Shazam shortcut. Body:

```json
{"title": "...", "artist": "...", "apple_music_id": "...", "shazam_url": "..."}
```

Response: `{"ok": true, "message": "<title> by <artist> added to new songs", "isrc": "..."}`
on success, or `{"ok": false, "message": "..."}` with a flag filed to Notion
when the track can't be matched on Spotify. An expired Spotify refresh token
returns `503` and also files a flag.

## Commands

Standard verb set (see global AGENTS.md) - the justfile is the interface,
not a script catalog; one-offs go in `scripts/` and run directly.

| Command | Purpose |
|---|---|
| `just dev` | Live-reload dev against real Modal infra (`modal serve`) |
| `just test` / `just check` / `just fmt` | pytest / ruff read-only / ruff fix |
| `just logs` | Stream deployed-app logs |
| `just sync-secrets` | Push `.env.tpl` -> Modal secret store |
| `just deploy` | test + sync-secrets + `modal deploy` |

## Manual setup (the only steps that can't be codified)

1. **Spotify developer app** - "AI Agent" at
   [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard),
   redirect URI `http://127.0.0.1:8080/callback`. Post-2026-02 apps in
   Development Mode get a reduced endpoint set (see spec section 7.1); save
   the client id/secret into the `Music Sync ENV` 1Password item.
2. **Spotify auth** - mint the refresh token (opens a browser, prints the
   value to paste into 1Password; nothing touches disk):
   ```
   op run --env-file=.env.tpl -- uv run scripts/spotify_auth.py
   ```
   Spotify expires this refresh token every 180 days. A run that sees
   `invalid_grant` flags it in Notion and stops; re-mint with the command
   above.
3. **Modal proxy-auth token for the Shazam shortcut** - minted once in the
   Modal dashboard under Settings -> Proxy Auth Tokens, stored as `MODAL_KEY`
   / `MODAL_SECRET` in `iOS Shortcuts ENV`. The shortcut posts to `/capture`
   with those headers.
4. **`op-project-bootstrap`** - fills the `Music Sync ENV` 1Password item and
   mints the Modal CI token, both via `scripts/provision.py`:
   ```
   op-project-bootstrap ~/Desktop/coding/active-projects/music-sync/.env.tpl --repo alexjmiller5/music-sync
   ```
5. **Modal auth** (before any local deploy): `uv run modal token new`

## Secrets

`.env.tpl` holds 1Password `op://` references only, pointing at the
`Music Sync` vault's `Music Sync ENV` item. Run everything through
`op run --env-file=.env.tpl -- <cmd>`. The hub token stored there
(`LIFE_HUB_TOKEN`) is named `music-sync-modal` on the hub, scoped
`tables:write` - life-data is written ONLY by this app; agents and the user
write Spotify directly (see AGENTS.md).

Reconciliation is gated by `RECONCILE_ENABLED=1` in the `music-sync` Modal
secret. The canonical `.env.tpl` includes it and `scripts/provision.py`
initializes it to `0`. Cron and mutating on-demand requests (including an
empty body) return `{"skipped": true}` while disabled. Explicit
`{"dry_run": true}` remains available and performs no writes or flag filing.
Capture is separately authorized and does not require activation.

After manual compliance review and a fresh dry-run, set the environment
item's `RECONCILE_ENABLED` field to `1` and sync secrets. Ordinary secret
syncs preserve this value because it is in the manifest. Do not activate
until every proposed Spotify mutation is accepted. `planned` contains each
action's playlist ID/name, ISRC/title when known, URI, reason and row/text
change. `applied` counts only confirmed batches, and is empty for dry runs;
the text summary separates Spotify mutations from mirror patches.

## Preservation and recovery

Cron, manual reconcile and capture synchronously dispatch to the same Modal
`worker`, configured with `max_containers=1` and
`@modal.concurrent(max_inputs=1)`. The entire read/archive/plan/apply cycle
runs there. FastAPI is a production dependency, including its real error
responses in the `--no-dev` image. Local migration imports must be run while
normal reconciliation is disabled and capture traffic is paused.

`run.reconcile(..., writes=False)` is an observation-only import: full owned
playlist pulls, actual liked values and complete observed membership rows.
It does not enforce auto-like, FIFO, dedupe, relink, undo, rules or expiry.
New owned playlists are classified curated before event detection during
normal enforcement, so their initial additions get liked exactly once.

Every reconcile (including recovery) archives a fresh raw pull under
`raw/spotify-pull/`; captures archive the inbox before adding or trimming
under `raw/spotify-capture/`. Unique object names avoid overwriting backups.
Archive failures stop before Spotify mutation.

Before applying a plan, the worker stores its remaining batches and complete
planned actions in `music-sync/pending-reconcile.json.gz` in the same R2
bucket. It checkpoints after each successful batch, stops at the first
Spotify, hub or checkpoint failure, and clears the object to JSON null only
when all batches finish. A new run resumes this plan before adopting a new
mirror baseline. Adds check live URI presence, so retries after uncertain
responses or partial 100-item client batches do not add duplicates. Same-URI
repair retains the re-add intent across crashes. Hub patches merge by ID
and transmit only exact sets of present columns, preserving undo identity
and timestamps; explicit null remains an intentional update.

An incomplete mutating plan blocks capture and observation imports until
reconciliation recovers. A failed observation import can resume with
`writes=False`. Recovery finishes the saved intent first; Spotify edits made
after a failed run are reconciled on the subsequent fresh run. A persistently
failing operation requires operator attention; do not delete pending evidence
or advance the mirror to bypass it.

The archive token needs object **read and write** permission on the configured
bucket; provisioning requests both. Existing write-only credentials need
replacement before running this version. Keep `music-sync/` outside raw
archive lifecycle expiration, and reserve its pending object for this one
worker/account. No new hub table, schema or recovery dependency is needed.

### Without 1Password

Plain env vars work everywhere `op run` is shown - export the fields listed
in `.env.tpl` instead. Mint the refresh token with
`uv run scripts/spotify_auth.py --client-id ... --client-secret ...` and
export the token it prints.
