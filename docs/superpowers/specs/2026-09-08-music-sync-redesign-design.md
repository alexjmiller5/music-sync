# Music Sync redesign - design spec

**Date:** 2026-09-08
**Status:** approved in chat, awaiting written review
**Metadata amendment:** [Preserve observed Spotify metadata](2026-09-12-observed-spotify-metadata-design.md)
records the approved ownership correction; its detailed written design is
awaiting review and is not yet implemented.
**Supersedes:** the GDPR-export → Notion sandbox prototype in this repo (everything under `src/core/` except `spotify_client.py`, all `scripts/`, `sandbox_config.json`).

## 1. What this is

Music Sync keeps a personal music catalog in life-data and materializes
**smart playlists** in Spotify from rules over that catalog. Spotify stays the
only app the user touches day to day. A single serialized Modal worker owns capture and reconciliation writes.
The hourly cron and authenticated endpoints dispatch to that worker.

The user's stated use case: sort music into playlists automatically, and create
new playlists by filtering the pool on genre and era, which Spotify cannot do.

## 2. Principles

1. **One writer per store.** Spotify is written by the user (the app), the
   app's `/capture` endpoint on the shortcut's behalf, and agents (via the
   `spotify` skill). life-data is written
   only by the serialized Modal worker, with one exception: an agent writes
   `playlists.rule` (and `kind`, `pinned`, `expires_at`) when the user asks
   for a smart playlist. The user and agents otherwise treat life-data as
   read-only.
2. **Spotify is a client, life-data is the record.** Membership of curated
   playlists is the user's curation and is mirrored in; smart playlist
   membership is computed from rules and projected out.
3. **The recording is the entity.** Row identity is the ISRC, never a Spotify
   track id (22% of the user's library has already been relinked to a
   different Spotify id; 208 recordings appear under 2+ ids).
4. **Facts come from sources, not from an LLM.** Genres and years are derived
   from Spotify, Deezer, and MusicBrainz by hub derivations.
5. **Vocabularies are catalog options.** `playlists.kind` is a select; no
   `<x>_types` table exists (life-map authoring standard, rule 4).

## 3. Identity

* `songs.id` = ISRC (12 chars, uppercase, pattern `^[A-Z]{2}[A-Z0-9]{3}[0-9]{7}$`).
* A recording that Spotify returns without an ISRC is skipped and counted in
  the run log (2 of 6,718 in the export; both were dead tracks). Local files
  (`is_local`) are skipped the same way.
* Different versions (radio edit, remix, live, remaster) carry different ISRCs
  and are different rows by design. A same-recording re-release under a second
  ISRC is tolerated; a `provenance` edge `rel='same_recording'` may link the
  two later, and the reconciler then treats them as one for dedupe. Not MVP.
* Greyed-out songs: Spotify still returns full metadata and ISRC for
  unavailable tracks (`is_playable=false`, restriction `market`). They import
  normally. The reconciler replaces an unplayable Spotify id in any playlist
  with a playable id of the same ISRC when one exists; otherwise the song is
  flagged.

## 4. Data model (life-data catalog)

All tables created with `life table create` typed specs and described with
`life table set` / `life property set` per the life-map authoring standard.
Table docs below are the catalog descriptions to be written verbatim.

### 4.1 `songs`

One row per recording the user has ever liked or placed in an owned playlist
(including songs that have since left every playlist - rows are never hard
deleted). Owner: music-sync cron. Consumers: music-sync, life-ui.

| col               | type     | req | constraint                                                                | description                                                                                                                                                                |
| ----------------- | -------- | --- | ------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| id                | text     | yes | immutable, pattern above                                                  | ISRC.                                                                                                                                                                      |
| liked             | int      | yes | 0/1, default 0                                                            | In Liked Songs at last reconcile. Liked ⇔ in the pool.                                                                                                                     |
| liked\_at         | datetime |     |                                                                           | Spotify `added_at` of the like.                                                                                                                                            |
| first\_seen       | datetime | yes |                                                                           | First reconcile that saw this recording anywhere.                                                                                                                          |
| title             | text     |     | derived `http:spotify_isrc` from id                                       | Spotify's name for the preferred track.                                                                                                                                    |
| artists           | json     |     | derived `http:spotify_isrc`                                               | Artist names, primary first.                                                                                                                                               |
| album             | text     |     | derived `http:spotify_isrc`                                               |                                                                                                                                                                            |
| album\_year       | int      |     | derived `http:spotify_isrc`                                               | Year of the album Spotify prefers for this ISRC.                                                                                                                           |
| duration\_ms      | int      |     | derived `http:spotify_isrc`                                               |                                                                                                                                                                            |
| spotify\_ids      | json     |     | derived `http:spotify_isrc`                                               | Every Spotify track id Spotify's ISRC search returns, preferred (playable, most markets) first.                                                                            |
| spotify\_playable | int      |     | derived `http:spotify_isrc`                                               | 1 if any id is playable in the user's market.                                                                                                                              |
| deezer\_genres    | json     |     | derived `http:deezer_isrc`                                                | Deezer's album genre names verbatim (Pop, Rock, Rap/Hip Hop, ...).                                                                                                         |
| deezer\_year      | int      |     | derived `http:deezer_isrc`                                                | Deezer release year of the matched track.                                                                                                                                  |
| mb\_tags          | json     |     | derived `http:musicbrainz_isrc`                                           | MusicBrainz recording tags with count > 0, lowercased, verbatim.                                                                                                           |
| mb\_first\_year   | int      |     | derived `http:musicbrainz_isrc`                                           | MusicBrainz `first-release-date` year of the recording.                                                                                                                    |
| first\_year       | int      |     | derived `http:first_year` from album\_year, deezer\_year, mb\_first\_year | `min` of the non-null inputs. Known ceiling: a remaster registered under its own ISRC reports the remaster's year when no source knows the original (2 of 25 in a sample). |

Doctrine rules: `songs-liked-is-pool` (liked=1 means eligible for every
playlist; liked=0 means the reconciler removes the song from every playlist
except the inbox). `songs-never-deleted` (rows are history; `deleted_at` is
never set by the reconciler).

Provenance: the reconciler writes `provenance` edges for capture:
`to_kind='songs'`, `to_ref=<isrc>`, `rel='captured_by'`, `from_kind` one of
`shazam` / `playlist` / `like`, `from_ref` = the playlist id or `liked`,
`asserted_by='music-sync'`, `observed_at` = Spotify `added_at`. The
"shazamed" fact is this edge, never a column.

### 4.2 `playlists`

One row per Spotify playlist the user owns. Owner: music-sync cron for every
column except `kind`, `rule`, `pinned`, `expires_at`, which an agent writes on
the user's instruction. Consumers: music-sync, life-ui.

| col              | type     | req | constraint                                                                 | description                                                                                                                                                                          |
| ---------------- | -------- | --- | -------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| id               | text     | yes | immutable                                                                  | Spotify playlist id. A smart playlist is created in Spotify first (by the agent, via the `spotify` skill) so the row is inserted with its real id; the cron never creates playlists. |
| name             | text     | yes |                                                                            | Spotify name, mirrored.                                                                                                                                                              |
| kind             | select   | yes | `inbox`, `curated`, `smart`; default `curated`                             | See §5. Exactly one row has kind `inbox`.                                                                                                                                            |
| rule             | json     |     | valid rule JSON (§6), required when kind=smart, null otherwise (invariant) | The smart playlist definition.                                                                                                                                                       |
| description      | text     |     |                                                                            | Spotify description as last written by the cron (smart) or mirrored (others).                                                                                                        |
| snapshot\_id     | text     |     |                                                                            | Spotify snapshot id at last reconcile; unchanged snapshot ⇒ playlist skipped.                                                                                                        |
| track\_count     | int      |     |                                                                            | Mirrored.                                                                                                                                                                            |
| pinned           | int      | yes | 0/1, default 1                                                             | 0 = ephemeral smart playlist, deleted at `expires_at`.                                                                                                                               |
| expires\_at      | datetime |     |                                                                            | Set when pinned=0; default 30 days after creation.                                                                                                                                   |
| last\_reconciled | datetime |     |                                                                            |                                                                                                                                                                                      |

Invariants: `playlists-one-inbox`, `playlists-rule-iff-smart`.

### 4.3 `playlist_songs`

Junction, one row per (playlist, recording) as of the last reconcile. Owner:
music-sync cron.

| col                | type            | req | description                             |
| ------------------ | --------------- | --- | --------------------------------------- |
| id                 | text            | yes | `<playlist_id>:<isrc>` (deterministic). |
| playlist\_id       | ref → playlists | yes |                                         |
| isrc               | ref → songs     | yes |                                         |
| spotify\_track\_id | text            | yes | The id actually in the playlist.        |
| added\_at          | datetime        | yes | Spotify `added_at`.                     |

Soft delete on removal; the 7-day undo (§5.4) reads soft-deleted rows.

Invariants for `life check`: `curated-songs-are-liked` (every non-deleted
row whose playlist is `curated` references a song with liked=1) and
`smart-songs-match-rule` (one generic SQL over the JSON predicates, §6.3).
Both only catch reconciler bugs; the reconciler is the enforcement.

### 4.4 Followed playlists, followed artists

Not modeled. Followed playlists are other people's; followed artists are a
later concern (the concerts relation task). The pull ignores playlists whose
owner is not the user.

## 5. Playlist kinds and the reconcile semantics

### 5.1 Kinds

* **inbox** - exactly one, named `new songs`. Capped at 100; the reconciler
  removes the oldest by `added_at` beyond 100. Songs arrive by the user adding
  directly, or from the Shazam shortcut. Membership implies nothing: songs in
  the inbox are not liked by being there, and un-hearting does not remove
  from it. Songs that age out unliked remain in `songs` with their capture
  edge (permanent Shazam history without clutter).
* **curated** - every other playlist the user edits by hand (feel good, love
  songs, 🍄, karaoke, ski, artist and event playlists). A curated playlist is
  the user's tag. The reconciler never adds or removes songs here except as
  consequences of hearting rules (§5.3) and unplayable relinking (§3).
* **smart** - membership = rule over the pool. Fully materialized each run:
  desired minus actual is added, actual minus desired is removed. Description
  is rewritten each run (§6.2). The user's three big buckets become the first
  smart playlists after migration (§9): `the good stuff` = pool, first\_year ≥
  2000; `galaxy` = pool, first\_year < 2000; `rap` = pool, deezer\_genres any
  `Rap/Hip Hop`.

### 5.2 The pool

Pool = songs with liked=1. The heart in the Spotify app is the only gesture
needed for a song to be sorted into every smart playlist it matches.

### 5.3 Events, detected by diffing live Spotify state against the mirror

The reconciler never acts on state alone; it compares the live pull with
`songs.liked` / `playlist_songs` from the previous run.

| Observation (mirror → live)                                         | Meaning                                          | Action                                                                        |
| ------------------------------------------------------------------- | ------------------------------------------------ | ----------------------------------------------------------------------------- |
| not in curated playlist → in it, liked=0                            | user added an unliked song to a curated playlist | like it (PUT /me/tracks), set liked=1, capture edge `playlist`                |
| liked=1 → liked=0, song in ≥1 non-inbox playlist                    | user un-hearted                                  | remove from every curated and smart playlist (memberships of playlists skipped this run come from the mirror); soft-delete junction rows; log |
| liked=1 → liked=0 and → added to a curated playlist in the same run | conflicting gestures                             | un-heart wins; log both                                                       |
| liked=0 → liked=1                                                   | user hearted                                     | pool membership; smart playlists recompute; capture edge `like` if first seen |
| soft-deleted curated rows < 7 days old, song liked again            | undo                                             | restore those curated memberships in Spotify and un-delete the rows           |
| `POST /capture` called                                              | Shazam capture (§7.3)                            | resolve on Spotify, add to inbox, capture edge `shazam`, trim inbox           |
| inbox count > 100                                                   | inbox overflow                                   | remove oldest beyond 100                                                      |
| smart playlist contains a song not matching its rule                | hand-add to a smart playlist, or rule changed    | remove; log ("removed N you had added by hand")                               |
| playlist song with `is_playable=false`                              | greyed out                                       | swap to a playable id of the same ISRC if one exists; else flag               |
| same ISRC twice in one playlist                                     | duplicate                                        | keep the earliest `added_at`, remove the other; when both copies share one Spotify URI, remove then re-add the kept copy (Spotify deletes every occurrence of a URI) |
| smart playlist pinned=0 past `expires_at`                           | ephemeral expiry                                 | delete in Spotify, soft-delete row                                            |

### 5.4 Undo

Removal soft-deletes `playlist_songs`. Re-liking within 7 days restores
curated memberships from those rows. Smart memberships recompute anyway.

### 5.5 Flags → Notion

Anything the reconciler cannot fix itself is batched into ONE Chore task per
run in the Tasks DB (data source id from config), Priority High, due the same
day, linked to the Music Sync project, listing each item on its own line:
unplayable songs with no alternative, songs without ISRC that were in a
playlist, a smart rule that fails validation, an inbox playlist that is
missing, Spotify or hub errors that aborted part of a run. No task when there
is nothing to flag. A run that flags the same item as the previous open task
appends to that task rather than creating another.

## 6. Rules

### 6.1 Rule JSON, version 1

```JSON
{
  "v": 1,
  "deezer_genres_any": ["Rap/Hip Hop"],
  "mb_tags_any": ["house", "deep house"],
  "first_year": {"gte": 1990, "lt": 2000},
  "in_playlist_any": ["feel good"],
  "not_in_playlist": ["😴"],
  "captured_by": "shazam",
  "liked_after": "2025-01-01"
}
```

Every key optional except `v`; keys AND together; list values OR within a
key. `first_year` accepts `lt`, `gte`, `between: [a, b]`. Playlist references
are by name and resolved to ids at reconcile time; an unresolvable name is a
flag. The pool (liked=1) is always implied. Validation is a catalog invariant
on `playlists.rule`: `json_valid`, `v = 1`, no keys outside this set, correct
value types. Unknown keys fail validation rather than being ignored.

### 6.2 Description rendering

`smart · genre: Rap/Hip Hop · year < 2000 · synced 2026-09-08`. Segments in
key order above, omitted when absent; `in:` and `not in:` for playlist
predicates; `shazamed` for captured\_by. Truncated to 300 characters. The cron
rewrites it every run and does not read it back.

### 6.3 Rule → SQL

One function renders a rule to a `WHERE` clause over `songs` (joined with
`playlist_songs` for the playlist predicates and `provenance` for
`captured_by`). The same renderer produces the generic `life check`
invariant by iterating `playlists WHERE kind='smart'` and unioning the
mismatches. No per-playlist rule objects exist in the catalog.

## 7. Runtime

### 7.1 Modal app `music-sync`

* `reconcile` - `modal.Cron("0 * * * *")` (hourly), also exposed as a
  proxy-auth `POST /reconcile` endpoint so the user, an agent, or the Shazam
  shortcut can trigger a run immediately. All entrypoints dispatch synchronously through `worker.remote(...)` to one
  worker with `max_containers=1` and `@modal.concurrent(max_inputs=1)`; a run skips curated and inbox playlists whose snapshot id is
  unchanged; smart playlists are always pulled (hearting changes no
  snapshot, and their membership is what the run materializes). Uses one of the five Starter-plan cron slots; `modal app list`
  before deploy confirms a slot is free.
* `capture` - proxy-auth `POST /capture` (§7.3). The one path that writes
  life-data outside the hourly run; same serialized worker, credentials and core code. FastAPI is declared in
  production dependencies for the `--no-dev` image.
* Activation: `RECONCILE_ENABLED` is in the canonical secrets manifest and
  provisioned as `0`. Both cron and mutating manual runs require `1`; explicit
  dry runs remain available while disabled. Capture is independently authorized.
* Secrets (`Music Sync ENV`): `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`,
  `SPOTIFY_REFRESH_TOKEN` (the "AI Agent" developer app; read scopes minted
  2026-09-07; Spotify expires it every 180 days - re-mint with
  `scripts/spotify_auth.py`, and a run that sees `invalid_grant` flags it in
  Notion and stops), `LIFE_HUB_URL`, `LIFE_HUB_TOKEN` (scope `tables:write`, minted
  with `life token create music-sync --scopes tables:write`), `NOTION_TOKEN`,
  `NOTION_TASKS_DATA_SOURCE_ID`, `NOTION_PROJECT_PAGE_ID`. `NOTION_PARENT_PAGE_ID`
  is deleted from the item.
* Spotify access uses only endpoints available to post-2026-02 Development
  Mode apps: `/me/playlists`, `/playlists/{id}`, `/playlists/{id}/items`,
  `/me/tracks`, `/tracks/{id}`, `/search?type=track&q=isrc:` (limit ≤ 10),
  `PUT/DELETE /me/tracks`, `POST/DELETE /playlists/{id}/items`, `PUT
  /playlists/{id}` (description), `POST /me/playlists`. No batch endpoints.
* life-data access is the hub HTTP protocol: `/v1/rows/pull` (with cursors)
  to load the mirror, `/v1/rows/push` for writes, `/v1/derive` for backfill.
  The client sends exact-key patch groups so omitted fields remain unchanged.
  It adds an absent `updated_at` once per push invocation in UTC milliseconds,
  preserving supplied values. Spotify `added_at` and `liked_at` are normalized
  to the same format ending in `Z` on the wire, including saved recovery batches.
  JSON objects and arrays remain unchanged; the hub stores them as JSON text.
  Catalog invariants are operator checks, not assumed write-time validation;
  the client validates rules before evaluation. Derivations enrich new rows.

### Failure recovery and review preservation

Before applying any batch, save its remaining operations and complete planned
actions to `music-sync/pending-reconcile.json.gz` in the existing R2 bucket.
Keep this object outside raw archive lifecycle expiry. Checkpoint after each
successful batch; on any Spotify, hub or checkpoint failure stop dependent
operations and retain durable evidence. Do not advance the song/membership
baseline after a failed Spotify batch. Partial hub writes are completed from
the pending plan before another pull is adopted as the baseline.

R2 objects use boto3's S3 API with bucket object read/write permissions.
`R2_ACCESS_KEY_ID` is the ID of `R2_API_TOKEN`; derive the secret access key
as SHA-256 of its value in memory. Provision the value before looking up its ID.
Only `NoSuchKey` represents absence; all other archive errors stop the flow.

Every resumed run archives a fresh full pull before replay. Add operations
check live URI presence before retrying uncertain or partially applied client
chunks. Relink additions precede old-URI removal. Same-URI dedupe repair keeps
one durable re-add intent after removal, including across worker restarts.
An incomplete mutating plan blocks capture and observation imports. Failed
observation imports can resume with `writes=False`. Recovery finishes saved
intent first; later user gestures are detected on the next fresh reconcile.
A persistent failure requires operator attention, never deletion of evidence.

Resolve final membership before dedupe repair: un-heart, smart exclusion and
expiry prevent resurrection, and any number of same-URI duplicates produces
at most one kept-copy re-add. Newly discovered owned playlists are curated
before event detection in enforcement mode. Seven-day undo rows retain full
identity and original `added_at`, including a new add conflicting with un-heart.

Dry runs return each structured planned action (playlist ID/name, ISRC/title
where known, URI, reason, row or text change), separated from confirmed applied
counts and routine mirror patches. They write nothing, including flags. R2
credentials require object read and write; no extra table or dependency is
introduced for recovery.

### 7.2 Derivations (repo `derivations`, Modal)

Four new endpoints, same protocol as `/movie`: `POST /spotify_isrc`,
`/deezer_isrc`, `/musicbrainz_isrc` (inputs.id = ISRC), `/first_year`
(inputs album\_year, deezer\_year, mb\_first\_year). MusicBrainz requires a
descriptive User-Agent and 1 request/second: that function runs with Modal
`concurrency_limit=1` and sleeps to 1.1 s per call. Deezer is unauthenticated.
`/spotify_isrc` uses client-credentials with the Music Sync app (search needs
no user scope) and `market=US`. Backfill of \~12,000 songs: `POST /v1/derive`
in chunks of 50 from a script; MusicBrainz alone takes \~3.5 hours; the hub's
15-minute sweep handles everything after.

The podcasts migration task (Life Data project) wants `/spotify_episode`;
same repo, shares the Spotify client. Out of scope here.

### 7.3 Shazam shortcut → `POST /capture`

The shortcut becomes a thin client of the app. `startShazam` → one
`jsonRequest` to the proxy-auth endpoint `POST /capture` with body
`{"title", "artist", "apple_music_id", "shazam_url"}` and the `Modal-Key` /
`Modal-Secret` headers (token minted once in the Modal dashboard, stored as
two fields in `iOS Shortcuts ENV`) → `showNotification` of the response
`message`. Shortcuts' Shazam result exposes no ISRC (only Apple Music ID,
title, artist), so the endpoint resolves the recording: Spotify search
`track:<title> artist:<artist>` (limit 10), best match by normalized title and
artist, ISRC from the result. It first reads and archives the live inbox under a unique
`raw/spotify-capture/` object key, then adds the track if absent, pushes the
`songs` row (if new) and the `captured_by=shazam` edge with the Apple Music
ID and Shazam URL in the edge `detail`, trims the inbox to 100, and returns
`{"ok": true, "message": "<title> by <artist> added to new songs"}`. A song
already in the live inbox still completes any missing catalog/provenance
writes and FIFO trim before returning ok with "already in new songs". Archive
failure prevents mutation. Pending reconciliation blocks capture until recovery. No match
returns `{"ok": false, "message": ...}` and files the Chore task ("add

<title> by <artist> to new songs manually") that the shortcut files via
Receptor today. `invalid_grant` on the app's token flags and returns an
error the user sees.

Consequences: the phone holds no Spotify credentials; `SPOTIFY_*` and
`SPOTIFY_SHAZAM_PLAYLIST_ID` leave `iOS Shortcuts ENV`; the `Spotify Reauth`
shortcut and its design spec are deleted; the Hammerspoon hyper+S binding
keeps running the same (rewritten) shortcut on the Mac. `My Shazam Tracks`
becomes an ordinary curated playlist; its 1,021 existing songs get
`captured_by=shazam` edges during migration from the playlist's `added_at`.
The shortcut rewrite (ios-shortcuts repo, `cherri` skill) ships in the same
milestone as the endpoint and is E2E-tested from the phone before the old
one is deleted.

### 7.4 Agents

Ad hoc Spotify writes (a 100-song party playlist) go through the `spotify`
skill (`spotify_player playlist edit --track-id … add <playlist>`), hydrating
one `-C` cache folder for the batch so 1Password is read once. The next
reconcile likes the songs and mirrors them. Smart playlist creation is an
agent running `spotify_player playlist new <name>`, then writing one
`playlists` row (`id` = the new Spotify id, `name`, `kind=smart`, `rule`,
`pinned`, `expires_at`) via `life insert` and running `life sync`; the cron
fills it and stamps the description on the next run.

## 8. Repo layout after the change

```
app.py                       Modal shim: reconcile cron + /reconcile endpoint
src/core/spotify_client.py   kept; extended with items/like/description/create calls
src/core/hub.py              life-data hub HTTP client (pull, push, derive)
src/core/mirror.py           load the mirror (songs, playlists, playlist_songs) from the hub
src/core/rules.py            rule JSON validation, → SQL, → description
src/core/reconcile.py        pure diff: (mirror, live) → list of actions
src/core/actions.py          apply actions to Spotify and the hub; run log
src/core/flags.py            batch flags into one Notion task
src/core/capture.py          /capture: resolve a Shazam result, add to inbox, record the edge
scripts/migrate.py           one-time §9 steps, each idempotent, --dry-run
scripts/review.py            §9.7 non-compliance report over the mirror (read-only)
scripts/backfill_derive.py   chunked /v1/derive with pacing
tests/                       reconcile diff cases, rule rendering, FIFO, undo, flags
```

Deleted: `export_ingest.py`, `notion_sync.py`, `playlist_builder.py`,
`models.py`, `pipeline.py`, every current script, `sandbox_config.json`,
`data/snapshot.json`. `data/raw/*.zip` moves to R2
`life-data-archive/raw/spotify-export/` (originals are sacred) and the local
copies are removed.

## 9. Migration (one-time, `scripts/migrate.py`, dry-run first)

1. Create catalog tables and rules (§4) via the `life` CLI; document them in
   life-map; regenerate schema.md.
2. Create the `new songs` playlist in Spotify; insert its row with kind
   `inbox`.
3. Mint the Modal proxy-auth token, add it to `iOS Shortcuts ENV`, rewrite
   the Shazam shortcut per §7.3, recompile, reinstall, delete the Reauth
   shortcut and the Spotify fields from `iOS Shortcuts ENV`.
4. **Backup.** The first pull's raw Spotify responses (every owned playlist
   with items, Liked Songs, the playlist list) are archived verbatim as one
   gzipped JSON object to R2 `life-data-archive/raw/spotify-pull/<date>.json.gz`
   before anything else happens, alongside the three GDPR export zips. Every
   later reconcile archives its pull the same way (a few MB per run, kept by
   the bucket's lifecycle rules). The mirror in life-data is the queryable
   backup; the R2 objects are the originals.
5. First pull into the mirror: every owned playlist and liked songs →
   `songs`, `playlist_songs`, `playlists` (all `curated`), capture edges
   (`like` from like `added_at`; `shazam` for `My Shazam Tracks` rows;
   `playlist` otherwise). No Spotify writes or enforcement planning. Observation imports preserve
   actual likes and all observed memberships, even on resumed imports and
   overflowing inboxes; no auto-like, FIFO, undo, dedupe, relink, rules or expiry.
6. Backfill derivations (§7.2). Nothing below depends on Spotify writes yet.
7. **Manual compliance review (gate).** `scripts/review.py` reads only the
   mirror and prints the non-compliance report, one section per category,
   with counts and a per-song listing (title, artists, year, the playlists it
   is in):
   * songs in curated playlists that are not liked (\~1,600 in the buckets)
   * liked songs in no playlist (398)
   * songs the bucket rules would remove or move (`first_year` disagrees
     with `the good stuff` / `galaxy`; not `Rap/Hip Hop` in `rap`)
   * unplayable songs, split into "alternate id exists" and "no alternate"
   * the same ISRC twice in one playlist (213 extra ids)
   * same title and artist under different ISRCs (142), for the user to mark
     as versions to keep or duplicates to collapse
   * `My Shazam Tracks` songs never liked (923), the inbox backlog
   * songs without ISRC or local files that were in a playlist
     The review happens in chat, in batches, finance-review style: the agent
     presents a section, the user decides (like, remove, keep as version,
     move, ignore), and the agent applies each decision **through the
     `spotify`** **skill**, never through life-data. Decisions that are rules
     rather than one-offs (for example "songs in `feel good` are always liked")
     are already the reconciler's behavior and need no action. The review ends
     when the report is empty or every remaining line is an accepted
     exception, and the accepted exceptions are written down in the run log.
8. Convert the three buckets to `smart` with the rules in §5.1.
9. Run `create_50s_playlist.py` once through the spotify skill flow into a
   curated `50s Gold` playlist, then delete the script (data never lives in
   code).
10. **Activation gate.** Re-pull, then run the reconciler in `--dry-run`. It
    must report zero proposed Spotify mutations other than the accepted
    exceptions from step 7; routine mirror patches are listed separately.
    Only then set `RECONCILE_ENABLED=1` and sync secrets. A disabled preview
    deployment is permitted before review. Confirm two clean hourly runs, then close the
    Notion tasks absorbed by this project (sync playlists, track following,
    download backup, dedup, Apple Music tag, 50s playlist).

## 10. Testing

* `reconcile.py` is a pure function and gets table-driven tests for every row
  of §5.3, plus FIFO, undo window, conflicting gestures, duplicate ISRC,
  unplayable relink.
* `rules.py`: validation rejects unknown keys and bad types; SQL and
  description rendering are golden-file tested; the generic invariant SQL is
  executed against a fixture SQLite db.
* Spotify and hub clients: mocked HTTP as in the existing
  `test_spotify_client.py`.
* Derivations: recorded responses per endpoint; MusicBrainz pacing asserted.
* E2E before migration step 7: run the reconciler against a `[test] smart`
  playlist with a narrow rule on the live account, confirm membership and
  description, delete it.
* Mutation check: break the un-heart branch and confirm the diff test fails.

## 11. Requirements (EARS)

1. The system shall identify every recording by ISRC and shall skip recordings without one, counting them in the run log.
2. When the reconciler runs, it shall pull every owned smart playlist and every curated or inbox playlist whose snapshot id changed and Liked Songs, and shall diff them against the mirror before acting.
3. When a song is added to a curated playlist while unliked, the reconciler shall like it and record a `playlist` capture edge.
4. When a liked song becomes unliked, the reconciler shall remove it from every curated and smart playlist and soft-delete its junction rows.
5. If a song is re-liked within 7 days of such a removal, the reconciler shall restore its curated memberships.
6. While a playlist has kind `smart`, its membership shall equal the rule's result over liked songs after each run, and its description shall render the rule.
7. The inbox playlist shall never exceed 100 songs; the reconciler shall remove the oldest beyond 100 and shall not like or remove songs there for any other reason.
8. When `/capture` receives a Shazam result, the system shall resolve the recording on Spotify, add it to the inbox, and record a `shazam` capture edge; if it cannot resolve it, it shall file a Chore task and add nothing.
9. Where a playlist song is unplayable and a playable Spotify id with the same ISRC exists, the reconciler shall swap ids; otherwise it shall flag the song.
10. When any condition it cannot resolve occurs, the reconciler shall write one Notion Chore task per run listing every flag, and none when there are no flags.
11. The reconciler shall reject invalid version-1 rule JSON before evaluating or materializing that playlist; catalog checks report persisted invalid rules.
12. The reconciler shall be the only writer of `songs`, `playlist_songs`, and every `playlists` column except `kind`, `rule`, `pinned`, `expires_at`.
13. The system shall use only Spotify endpoints available to Development Mode apps created after 2026-02-11.
14. The MusicBrainz derivation shall not exceed one request per second.
15. Hourly reconcile, manual reconcile and capture shall share one serialized worker covering the entire read/archive/plan/apply cycle.
16. The reconciler shall support a dry-run mode that reports every action it would take and applies none.
17. Before any Spotify write, each run shall archive its raw pull verbatim to R2.
18. Reconciliation writes shall remain disabled, including on-demand requests, until a fresh dry-run reports no proposed Spotify mutations beyond accepted review exceptions. The activation field shall default to 0 and survive secrets sync.
