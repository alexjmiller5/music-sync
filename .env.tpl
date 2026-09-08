# Canonical secrets manifest — 1Password secret references only, SAFE to commit.
# Refs are BY NAME on purpose: op-project-bootstrap parses this file.
# Local dev:       op run --env-file=.env.tpl -- <cmd>   (see justfile)
# Push to Modal:   just sync-secrets
NOTION_TOKEN=op://Music Sync/Music Sync ENV/NOTION_TOKEN
# Notion page id the sandbox page + DBs are created under (not secret, kept with the token)
NOTION_PARENT_PAGE_ID=op://Music Sync/Music Sync ENV/NOTION_PARENT_PAGE_ID
SPOTIFY_CLIENT_ID=op://Music Sync/Music Sync ENV/SPOTIFY_CLIENT_ID
SPOTIFY_CLIENT_SECRET=op://Music Sync/Music Sync ENV/SPOTIFY_CLIENT_SECRET
SPOTIFY_REFRESH_TOKEN=op://Music Sync/Music Sync ENV/SPOTIFY_REFRESH_TOKEN
