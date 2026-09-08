# music-sync

Two-way Spotify <-> Notion playlist sync, deployed on
[Modal](https://modal.com) from the `modal-service` template.

## Layout

```
app.py            Modal shim — image, secrets, endpoints, schedules
src/core/         business logic (plain Python, portable)
tests/            pytest
data/raw/         Spotify GDPR export zips (gitignored — personal data, NEVER commit)
.env.tpl          secrets manifest (1Password op:// refs, committed)
justfile          dev / test / sync-secrets / deploy
```

## Export ingestion

`uv run scripts/ingest_export.py [export.zip]` reads a GDPR "Account Data" zip
directly (no unzip; defaults to the newest zip in `data/raw/`) and writes a
normalized snapshot to `data/snapshot.json` (gitignored) — this snapshot is
the Spotify data backup. Request an export at
[spotify.com/account/privacy](https://www.spotify.com/account/privacy/).
Followed artists come from `YourLibrary.json` `artists` (the export's
`Follow.json` only contains follower *counts*).

## Notion sandbox sync

```
op run --env-file=.env.tpl -- uv run scripts/sync_playlists_to_notion.py
```

Syncs `data/snapshot.json` into sandbox **Playlists**/**Songs** databases that
live on a "Spotify Sync Sandbox" page created under the Notion page named by
`NOTION_PARENT_PAGE_ID` — no pre-existing Notion pages are touched.
`sandbox_config.json` (gitignored, generated on first run) pins the
page/database/data-source ids; delete it to recreate the
sandbox from scratch. Upserts are idempotent: playlists keyed by name (the
GDPR export has no playlist ids — Spotify ID stays empty until live API sync),
songs keyed by Spotify URI (fallback `artist|name`). A song in N playlists is
one Songs page with N relation links; relations added by hand in Notion
survive resyncs.

## Followed artists sync

```
op run --env-file=.env.tpl -- uv run scripts/sync_followed_to_notion.py
```

Upserts followed artists (from the export zip) into a sandbox **Followed
Artists** database on the same sandbox page, keyed on Spotify ID — reruns
create no duplicates. Genres/Followers stay empty until Spotify developer
creds exist; once they do, the same command fetches live data via
`src/core/spotify_client.py` (`GET /v1/me/following`) and enriches the same
rows. The client (token refresh, 401 refresh-retry, 429 Retry-After,
pagination for followed artists / playlists / playlist tracks) is fully
unit-tested against mocks and needs no code changes to go live.

## Go live + build the 50s playlist

The ~50-track list is staged for review as **"50s Gold (draft)"** in the
sandbox Notion Playlists DB (tracks related via the Songs DB) and committed as
data-in-code in `scripts/create_50s_playlist.py`.

1. Create the Spotify developer app (Manual setup step 1 below).
2. Mint the refresh token — opens a browser, then prints the exact
   `op item create`/`op item edit` command to paste yourself (the token never
   touches disk):
   ```
   op run --env-file=.env.tpl -- uv run scripts/spotify_auth.py
   ```
3. Build the playlist (creates ONE new private playlist, additive-only,
   touches nothing existing):
   ```
   op run --env-file=.env.tpl -- uv run scripts/create_50s_playlist.py
   ```

## Manual setup (the only steps that can't be codified)

1. **Spotify developer app** — at
   [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard)
   create an app with redirect URI `http://127.0.0.1:8888/callback`, then save
   the client id/secret to a **`<Project> Spotify OAuth Client`** item in `<your 1Password vault>`
   with fields `client_id`, `client_secret`, `refresh_token` (leave
   `refresh_token` blank — the auth script fills it).
2. **Spotify auth** — mint the refresh token:
   ```
   op run --env-file=.env.tpl -- uv run scripts/spotify_auth.py
   ```
3. **Modal auth** (before any deploy): `uv run modal token new`

## Secrets

`.env.tpl` holds 1Password `op://` references only — point them at items in
`<your 1Password vault>` and run everything through
`op run --env-file=.env.tpl -- <cmd>`.

### Without 1Password

Plain env vars work everywhere `op run` is shown — just export them instead:

```
export NOTION_TOKEN=ntn_...
export NOTION_PARENT_PAGE_ID=<notion page id for the sandbox>
export SPOTIFY_CLIENT_ID=... SPOTIFY_CLIENT_SECRET=... SPOTIFY_REFRESH_TOKEN=...
uv run scripts/sync_playlists_to_notion.py
```

Mint the refresh token with
`uv run scripts/spotify_auth.py --client-id ... --client-secret ...` and
export the token it prints.
