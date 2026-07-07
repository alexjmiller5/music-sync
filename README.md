# notion-spotify-sync

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

`uv run scripts/ingest_export.py` reads the GDPR "Account Data" zip in
`data/raw/` directly (no unzip) and writes a normalized snapshot to
`data/snapshot.json` (gitignored) — this snapshot is the Spotify data backup.
Note: the export dates from **July 2025 (~1 year stale)**; request a fresh one
at [spotify.com/account/privacy](https://www.spotify.com/account/privacy/) when
current data matters. Followed artists come from `YourLibrary.json` `artists`
(the export's `Follow.json` only contains follower *counts*).

## Notion sandbox sync

```
op run --env-file=.env.tpl -- uv run scripts/sync_playlists_to_notion.py
```

Syncs `data/snapshot.json` into sandbox **Playlists**/**Songs** databases that
live on the "Spotify Sync Sandbox" page under the Spotify Notion Sync project
page — no pre-existing Notion pages are touched. `sandbox_config.json`
(committed) pins the page/database/data-source ids; delete it to recreate the
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

## Manual setup (the only steps that can't be codified)

1. **Spotify developer app** — at
   [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard)
   create an app with redirect URI `http://127.0.0.1:8888/callback`, then save
   the client id/secret to a **`Spotify API`** item in the 1Password
   **OpenClaw** vault with fields `client_id`, `client_secret`,
   `refresh_token` (leave `refresh_token` blank — the auth script fills it).
2. **Spotify auth** — mint the refresh token:
   ```
   op run --env-file=.env.tpl -- uv run scripts/spotify_auth.py
   ```
3. **Modal auth** (before any deploy): `uv run modal token new`

## Secrets

`.env.tpl` holds op:// references only. Secrets currently live in the shared
**OpenClaw** vault (the claude-code service account can't mint vaults);
migrate to a per-project vault + `notion-spotify-sync-ci` service account
per the global secrets policy when convenient.
