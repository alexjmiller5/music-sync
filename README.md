# music-sync

Keeps a personal music catalog in life-data and materializes smart playlists
in Spotify from rules over that catalog, deployed on
[Modal](https://modal.com) from the `modal-service` template. Full design:
`docs/superpowers/specs/2026-09-08-music-sync-redesign-design.md`.

## Layout

```
app.py            Modal shim - image, secrets, endpoints, schedules
src/core/         business logic (plain Python, portable)
tests/            pytest
data/raw/         Spotify GDPR export zips (gitignored - personal data, NEVER commit)
.env.tpl          secrets manifest (1Password op:// refs, committed)
justfile          dev / test / sync-secrets / deploy
```

`src/core/spotify_client.py` and `src/core/hub.py` (the life-data hub HTTP
client: pull/push/derive) are the only `src/core/` modules so far; the
reconciler, rules engine, and mirror land in later tasks per the design spec.

## Manual setup (the only steps that can't be codified)

1. **Spotify developer app** - at
   [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard)
   create an app with redirect URI `http://127.0.0.1:8080/callback`, then save
   the client id/secret into the `Music Sync ENV` 1Password item.
2. **Spotify auth** - mint the refresh token (opens a browser, prints the
   value to paste into 1Password; nothing touches disk):
   ```
   op run --env-file=.env.tpl -- uv run scripts/spotify_auth.py
   ```
3. **Modal auth** (before any deploy): `uv run modal token new`

## Secrets

`.env.tpl` holds 1Password `op://` references only, pointing at the
`Music Sync` vault's `Music Sync ENV` item. Run everything through
`op run --env-file=.env.tpl -- <cmd>`.

### Without 1Password

Plain env vars work everywhere `op run` is shown - export the fields listed
in `.env.tpl` instead. Mint the refresh token with
`uv run scripts/spotify_auth.py --client-id ... --client-secret ...` and
export the token it prints.
