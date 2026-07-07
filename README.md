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
