# music-sync

Keeps a personal music catalog in life-data and materializes smart playlists
in Spotify from rules over that catalog, deployed on
[Modal](https://modal.com) from the `modal-service` template. Full design:
`docs/superpowers/specs/2026-09-08-music-sync-redesign-design.md`.

## Layout

```
app.py            Modal shim - image, secrets, the hourly reconcile, /reconcile and /capture endpoints
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
{"summary": "...", "applied": {"upsert_song": 3, "...": 0}, "flags": [], "errors": []}
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

The cron is gated by `RECONCILE_ENABLED=1` in the `music-sync` Modal secret;
until that field is set, `reconcile_cron` runs hourly but returns
`{"skipped": true}` without touching Spotify or life-data. The one-time
migration that flips it on lives in `scripts/migrate.py` (spec section 9).

### Without 1Password

Plain env vars work everywhere `op run` is shown - export the fields listed
in `.env.tpl` instead. Mint the refresh token with
`uv run scripts/spotify_auth.py --client-id ... --client-secret ...` and
export the token it prints.
