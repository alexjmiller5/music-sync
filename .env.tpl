# Canonical secrets manifest - 1Password secret references only, SAFE to commit.
# Refs are BY NAME on purpose: op-project-bootstrap parses this file.
# Local dev:       op run --env-file=.env.tpl -- <cmd>   (see justfile)
# Push to Modal:   just sync-secrets
SPOTIFY_CLIENT_ID=op://Music Sync/Music Sync ENV/SPOTIFY_CLIENT_ID
SPOTIFY_CLIENT_SECRET=op://Music Sync/Music Sync ENV/SPOTIFY_CLIENT_SECRET
SPOTIFY_REFRESH_TOKEN=op://Music Sync/Music Sync ENV/SPOTIFY_REFRESH_TOKEN
LIFE_HUB_URL=op://Music Sync/Music Sync ENV/LIFE_HUB_URL
LIFE_HUB_TOKEN=op://Music Sync/Music Sync ENV/LIFE_HUB_TOKEN
NOTION_TOKEN=op://Music Sync/Music Sync ENV/NOTION_TOKEN
NOTION_TASKS_DATA_SOURCE_ID=op://Music Sync/Music Sync ENV/NOTION_TASKS_DATA_SOURCE_ID
NOTION_PROJECT_PAGE_ID=op://Music Sync/Music Sync ENV/NOTION_PROJECT_PAGE_ID
R2_ACCOUNT_ID=op://Music Sync/Music Sync ENV/R2_ACCOUNT_ID
R2_BUCKET=op://Music Sync/Music Sync ENV/R2_BUCKET
R2_API_TOKEN=op://Music Sync/Music Sync ENV/R2_API_TOKEN
R2_ACCESS_KEY_ID=op://Music Sync/Music Sync ENV/R2_ACCESS_KEY_ID
RECONCILE_ENABLED=op://Music Sync/Music Sync ENV/RECONCILE_ENABLED
