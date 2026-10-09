# music-sync

Keeps a personal music catalog in soma and materializes smart playlists
in Spotify from rules over that catalog, deployed on
[Modal](https://modal.com) from the `modal-service` template. Full design:
`docs/superpowers/specs/2026-09-08-music-sync-redesign-design.md`.

## Layout

```
app.py            Modal shim - one serialized worker, hourly trigger and HTTPS endpoints
src/core/         business logic (plain Python, portable, no Modal imports)
  spotify_client.py  Spotify Web API client (post-2026-02 Development Mode endpoint set)
  hub.py              soma hub HTTP client (pull, push, derive)
  mirror.py           load the mirror (songs, playlists, playlist_songs) from the hub
  rules.py            rule JSON validation, -> SQL, -> description
  reconcile.py         pure diff: (mirror, live) -> list of actions
  actions.py          apply actions to Spotify and the hub; run log
  flags.py            batch flags into one Notion Chore task
  capture.py           /capture: resolve a Shazam result, add to inbox, record the edge
  capture_clients.py   capture-only credentials and idempotent delivery receipts
  config.py           Settings (env vars only)
  model.py             dataclasses shared across core
  archive.py           archive raw pulls through the soma file API
  metadata_replay.py   fill missing metadata from one retained Spotify archive
  curation.py          liked/curated transitions, auto-like intent, unlike exceptions
  history.py           occurrence-specific evidence for changed playlist contents
  recognition.py       retained recognition events and the songs Shazam summary
  smart.py             supported playlist kind/rule configuration (`just rules`)
  package.py           one-time rollout package: build, simulate, apply, verify
  soma_flags.py        Soma flag task batches and quiet review rows
  workspaces.py        per-person settings records; spotify_connect.py invites
scripts/
  provision.py       mints R2/Modal tokens; resolves R2_ACCESS_KEY_ID after R2_API_TOKEN
  sync_secrets.py     push .env.tpl -> Modal secret store
  spotify_auth.py     mint/re-mint the Spotify refresh token
  preview.py          read-only full dry-run receipt (locally or `--remote`)
  rollout.py          observation import, rollout package build/apply, recognition import
  rules.py            playlist kind/rule configuration (`just rules`)
  capture_clients.py  issue/revoke capture clients (`just clients`)
  workspace.py        workspace records (`just workspace`)
  backfill_derive.py  enrichment backfill through the hub's derivation API
  followed_artists.py read-only followed-artist receipt
tests/            pytest
data/raw/         Spotify GDPR export zips (gitignored - personal data, NEVER commit)
.env.tpl          secrets manifest (1Password op:// refs, committed)
justfile          dev / test / sync-secrets / deploy
```

## What it does

Every song lives in soma with `liked` = whether it's hearted in Spotify.
Playlists come in three kinds:

- **inbox** - exactly one, `new songs`, capped at 100. Songs land here from
  the user adding directly or from the Shazam shortcut; membership implies
  nothing (not liked by being there, not removed by un-hearting). Songs that
  age out unliked stay in the catalog with their capture history.
- **curated** - every other playlist the user edits by hand. The reconciler
  preserves membership independently of likes. Duplicate keepers and
  replacement aliases require review.
- **smart** - membership = a rule over the pool (every liked song), fully
  materialized each run; the description is rewritten each run.

Liking a song makes it eligible for smart rules. Unliking never removes it
from a curated playlist, and re-liking never restores a removed curated item.
After a complete baseline, a newly curated unliked song is proposed for liking.
A recording previously observed both liked and curated, then unliked while
still curated, becomes one persistent review exception and is not silently
re-liked, including through another curated playlist. Each exception is
returned as a `review_item` and, when a Soma flag binding is configured,
delivered once as its own task row (create-only, no notification). Repeated
runs never create a second row, and a row the owner closes stays closed.

The first complete observation does not execute a migration of existing
curated-but-unliked songs; that one-time migration is a reviewed rollout
package (`scripts/rollout.py`). Unknown playlists need classification
(`just rules curated <id>`) before they can authorize auto-likes.

Songs carry a visible Shazam summary (`shazamed`, `shazam_count`,
`shazam_first_at`, `shazam_last_at`, `shazam_dates_estimated`) recomputed from
every retained recognition event. A repeat recognition is another event, never
another playlist entry; historical playlist add dates and capture receipt times
are labeled estimates.
Duplicate occurrences stay unchanged for keeper/metadata review; smart-rule
changes for a playlist with duplicate conflicts are held rather than partially
removing one alias.

## Endpoints

`/reconcile`, the legacy capture endpoint and capture-client administration
require Modal proxy auth (`Modal-Key` / `Modal-Secret` headers). The consumer
capture endpoint uses an app-issued Bearer token instead.

**`POST /reconcile`** - runs the reconciler on demand (same logic as the
hourly cron). Body:

```json
{"dry_run": false}
```

Response:

```json
{"summary": "...", "planned": [], "applied": {}, "flags": [], "errors": []}
```

For metadata-only recovery, send the same endpoint:

```json
{"metadata_replay":{"archive_key":"raw/spotify-pull/example.json.gz","observed_at":"2026-01-01T00:00:00.000Z"},"dry_run":true}
```

Replay defaults to dry-run. Applying requires the explicit JSON boolean
`"dry_run": false`; strings, numbers, null and extra request fields are rejected
with HTTP 422. `observed_at` is the archive's observation time, with an explicit
timezone, normalized to UTC milliseconds. Keys must be normalized `.json.gz`
objects below `raw/spotify-pull/` or `raw/spotify-capture/`; missing objects return
404. Both normalized pull envelopes and capture envelopes are accepted,
including captures without `resolved_track`.

Recovery uses the same serialized worker, without requiring
`RECONCILE_ENABLED=1`. It reads one retained archive and the current mirror,
fills missing base metadata on existing recordings, and unions observed track
IDs. It preserves nonempty facts, likes, membership, capture history,
`first_seen`, enrichment and existing provenance. Album facts are filled only
when both existing album fields are empty. It makes no Spotify or enrichment
provider requests. Evidence records the archive's explicit `market`, or null
when the retained envelope has no market; current configuration is not evidence
of the archive's market.

Responses contain `recovered`, `already_present`, `conflicting`, `missing_source`
and `failed` counters, per-ISRC `rows` (including field conflicts), structured
`planned` actions and `errors`, and confirmed batch counts in `applied`.
Each catalog or archived recording receives one outcome. Missing catalog rows,
recordings absent from this archive, and gaps without usable source metadata
are `missing_source`; replay never creates songs. Conflicts take precedence
over recovered even when other fields are filled. Dry-run counters describe
the proposal and `applied` stays empty. Failed plans are reported conservatively
as failed for affected rows; `applied` separately records confirmed fills.
Repeating a completed replay makes no further writes, including provenance or
timestamps. Dry runs write nothing to the hub, archives, Spotify or Notion.

Replay checkpoints use the existing pending object with `intent=metadata_replay`.
Pending reconciliation blocks replay with HTTP 409. A pending replay blocks
reconcile and capture before Spotify setup; resume through the replay request
with the same archive key and observation time. Its saved operations and
attribution survive partial hub failures and source retention expiry. Retry
does not replan after a successful song patch, so remaining provenance is kept.
Catalog read outages return HTTP 503. Do not delete pending intent to bypass
recovery.

**`POST /capture`** - used by the Shazam shortcut. Body:

```json
{"title": "...", "artist": "...", "apple_music_id": "...", "shazam_url": "..."}
```

Response: `{"ok": true, "message": "<title> by <artist> added to new songs", "isrc": "..."}`
on success, or `{"ok": false, "message": "..."}` with a flag filed to Notion
when the track can't be matched on Spotify. An expired Spotify refresh token
returns `503` and also files a flag.

**`POST capture-consumer endpoint`** - the endpoint URL labeled
`capture-consumer` in Modal deploy output. It does not require Modal provider
credentials. Send `Authorization: Bearer <capture-token>` and these five
required JSON fields:

```json
{
  "capture_id": "3d2ed84e-9413-4a4a-a7e1-c596201bf84d",
  "title": "...",
  "artist": "...",
  "apple_music_id": "...",
  "shazam_url": "..."
}
```

All fields are strings, `capture_id` is a UUID, and `title` and `artist` are
nonempty. When Shazam supplies an ISRC, add the optional string field
`"isrc":"USAAA2600001"`; hyphens and case are normalized. Music Sync searches
Spotify by that ISRC and accepts only a candidate carrying the same ISRC.
Without one, matching requires normalized exact title and artist identity,
including version words such as live or remix.

An optional `recognized_at` string records the source's timezone-aware ISO
timestamp, with its original precision. If omitted, recognition time remains
unknown; server receipt time is stored separately. Each workspace/client/capture
UUID retains one recognition event and its original request independently of
playlist membership. Retry the same UUID and payload; a real new recognition
uses a new UUID. Repeated recognition does not duplicate an existing inbox item.

Only a selected Spotify track with a valid ISRC, track ID and URI is stored
with the payload identity before Spotify or hub side effects. An incomplete
candidate is not stored, so the same capture UUID can resolve it on a later
retry. A successful capture is acknowledged only after that state is replaced
by a durable receipt in Music Sync's R2:

```json
{"ok":true,"capture_id":"3d2ed84e-9413-4a4a-a7e1-c596201bf84d","isrc":"USAAA2600001","spotify_outcome":"added"}
```

Repeating the same client, capture UUID and payload resumes the stored track or
replays its completed receipt without selecting a different recording. The
optional ISRC participates in payload identity. Reusing the UUID with a changed
payload returns `409`. Missing, invalid or revoked credentials return `401`;
malformed requests return `422`; storage or capture availability failures return `503`.
Only a response with HTTP 200, `ok: true`, the matching `capture_id` and a
nonempty `isrc` is a delivery acknowledgement.

Validated deliveries also report a `spotify_outcome` bound to the same
`capture_id`, independently of completion of catalog maintenance:

| Outcome | Evidence | Client behavior |
| --- | --- | --- |
| `added` | Spotify acknowledged the add, or the selected recording was observed in the inbox. | Keep successful adds silent, including after a later catalog failure. |
| `not_added` | This capture has never reached a Spotify mutation attempt. | A matching capture ID permits the add-failure alert; it does not claim the song is absent from Spotify. |
| `unknown` | An attempted add lacks acknowledgement, or an older incomplete receipt has no outcome evidence. | Retain retry state without claiming the song was not added. |

An HTTP error, timeout, missing outcome, or mismatched capture ID alone never
proves `not_added`. Retry the identical capture ID and payload, honoring
`Retry-After`. `ok: false` with `spotify_outcome: added` means catalog work
still needs retry, not that Spotify rejected the song. Keep already observed
`added` evidence on the client even if a later transport failure is ambiguous.
The service stores `unknown` before sending an add and `added` immediately
after acknowledgement or confirmed inbox membership. A receipt-write failure
stops later operations; it cannot undo an already acknowledged Spotify add.

**`POST capture-access endpoint`** - the endpoint URL labeled `capture-access`
in Modal deploy output. It requires the existing Modal proxy-auth headers.
Issue a client:

```json
{"action":"issue","label":"phone"}
```

The response is `{"ok":true,"client_id":"<uuid>","token":"<capture-token>"}`.
The token is shown only in this response. Revoke that client without affecting
other clients:

```json
{"action":"revoke","client_id":"<uuid>"}
```

The response is `{"ok":true,"revoked":true}`. Store the consumer endpoint URL
and returned token in the app's supported configuration and Keychain.

**Enrollment link** - `just clients issue "<device>"` issues a client through
the same function with operator Modal auth and prints a link to the static
`capture-enroll` page. The URL and token ride in the link's fragment (never
sent to a server); the page opens `offlineshazam://enroll?url=&token=`, which
Cochlea saves to its Keychain and verifies. `just clients revoke
<client_id>` revokes one device.

## Workspaces

Music Sync can act for more than one person without a user database. The
operator's own setup is the `default` workspace (env). Another person is a
workspace record holding their soma hub, Notion target and limits, plus
the Spotify refresh token they grant themselves through a **Connect Spotify**
link (`just workspace connect-link <id>`). Their Cochlea devices get
tokens bound to that workspace (`just clients issue "<device>" <id>`).
Workspace secrets are encrypted at rest with `WORKSPACE_SECRET_KEY`, a Fernet
key in the ENV item (`python -c "from cryptography.fernet import Fernet;
print(Fernet.generate_key().decode())"`). See AGENTS.md for the mechanics.

## Commands

Standard verb set (see global AGENTS.md) - the justfile is the interface,
not a script catalog; one-offs go in `scripts/` and run directly.

| Command | Purpose |
|---|---|
| `just dev` | Live-reload dev against real Modal infra (`modal serve`) |
| `just test` / `just check` / `just fmt` | pytest / ruff read-only / ruff fix |
| `just logs` | Stream deployed-app logs |
| `just sync-secrets` | Push `.env.tpl` -> Modal secret store |
| `just deploy` | test + sync-secrets + `modal deploy` (CI does this on push) |
| `just rules <action>` | Playlist kinds and smart rules (see Operating) |
| `just clients <action>` | Issue/revoke capture clients |
| `just workspace <action>` | Workspace records and Connect Spotify links |

**Resumable metadata backfill:** with `SOMA_HUB_URL` and `SOMA_HUB_TOKEN`
in the environment, run `uv run scripts/backfill_derive.py`. The same script
can run as `python -u scripts/backfill_derive.py` in a container with httpx
and `src/core` available. It calls only the hub's pull/derive interfaces.
`--col deezer_genres` or `--col mb_tags` narrows the enrichment source;
each also checks/refreshes `first_year`. `--col first_year` checks only years.
Use `--batch-size N` to tune normal source requests from 1 to 50; the default
is 50. `first_year` is automatically capped at 20 IDs per hub request because
that derivation also records provenance and must stay inside the hub's SQL
budget.

To repair an explicit set of inconsistent years, prepare a JSON array of
recording IDs outside the repository and preview it:

```sh
uv run scripts/backfill_derive.py --col first_year --ids-file /path/to/ids.json --refresh --dry-run
```

Bounded apply runs flush a `backfill_batch` JSON line after each checked batch,
including completed IDs, per-row failures and cooldowns. These are partial
receipts, not a claim that unattempted IDs completed; retain the full output.
The final JSON receipt lists `selected_ids`, `planned_ids`, and each row's
before, expected, after and status. Remove `--dry-run` to apply the exact
selection. Start with a one-ID sample and verify its live result before a
larger selection. `--refresh` bypasses matching-hash reuse only when an explicit
IDs file and `--col first_year` are supplied. Without `--refresh`, normal
provenance reuse is unchanged. Small selections and per-batch verification read exact song/proof IDs. Large
refresh previews use supported 200-row song pages and retain only selected IDs;
these pages are not a frozen snapshot. Empty, duplicate, malformed, missing or
deleted IDs fail without expanding the write selection. Derivation requests remain sequential and at most 20 IDs.

Refresh verifies the sole `http:first_year` output and expected inputs in the
catalog, then checks source inputs/likes for concurrent changes and the actual
result against the existing Derivations contract: coerce each source with `int`,
ignore conversion failures and values outside 1900-2100 inclusive, then take
the minimum accepted year. With none accepted, the endpoint omits `first_year`;
the receipt sets `expected_omitted` and refresh remains unresolved rather than
claiming a null write. Source values, including unknown zero values, remain
unchanged. A failed or inconsistent result
is reported without blind refresh retries. Structured cooldowns retain their
status and retry time, and defer unattempted IDs. This command never writes
source inputs, likes or memberships. Keep selection files and receipts outside
git, and use the existing project environment and caller credential.

Pending recordings are sent in batches of up to 50 IDs per normal source
request.
When the hub returns an exact partial failure, retries narrow to the failed
IDs. Counts alone never establish completion: current rows and matching source
proofs must confirm every required field. Empty or partial source results remain
unresolved; no new negative cache is created. Existing complete proofs can reuse
legitimate null years. Confirmed upstream writes trigger a final year refresh;
current year inputs and proofs are reread before deciding, including after partial
writes. Each failed pair gets at most three
attempts, waiting 5 then 15 seconds. A rate limit (`429`) or a service outage
(`503`) carrying `retry_after` immediately defers that source for the rest
of the run, without a short retry. The JSON summary's `retry_at` maps source
columns to Unix timestamps for the earliest retry; wait until that time
before resuming that source. Missing or malformed `429` delays use 60 seconds.
Five consecutive recordings exhausting
timeout/transport retries pause only that source for the run; record-specific
HTTP errors such as 502 do not pause it. Other records/sources continue.
Progress separates recordings, source writes, and reused checkpoints. The
last stdout line is JSON with unresolved `failed` entries, `stopped_sources`,
and `deferred` counts; failures/deferred work exit 1. Keep that output in the
job logs and rerun the same command to resume from provenance. No local
checkpoint files or service changes are needed.

Spotify base fields are observations, never backfill targets. `album_year` is
the observed Spotify release year; enriched `first_year` uses that year together
with Deezer and MusicBrainz evidence. `--col title` is rejected.
Mutating imports, capture, pending recovery, replay apply and enrichment backfill
require all seven base properties to exist without derivation bindings. Read-only
previews remain available before cutover (existing pending recovery interlocks
still apply). The live catalog is not asserted to have changed: follow the
[staged rollout](docs/observed-metadata-rollout.md) under separate approval.

## Operating

**Normal use.** Spotify is the editing interface: like songs, edit curated
playlists, Shazam with Cochlea. With `RECONCILE_ENABLED=1` the hourly cron
mirrors those changes into soma, likes newly curated songs, materializes
smart playlists and trims the inbox. Nothing about the catalog is edited by hand.

**Ad-hoc dry run.** `uv run scripts/preview.py --remote --output <private.json>`
runs a full read-only reconciliation in the deployed worker (operator Modal
auth: the `MODAL_TOKEN_ID`/`MODAL_TOKEN_SECRET` pair used by `just workspace`)
and writes a mode-0600 receipt. Without `--remote` it runs locally under
`op run --env-file=.env.tpl`. While Spotify is rate limiting the app,
`--observation <raw/spotify-pull/...>` plans from a retained complete pull
instead (a mutating run always takes a fresh pull). **Ad-hoc real run:** `POST /reconcile` with
`{"dry_run": false}` and the Modal proxy-auth headers; it still requires
`RECONCILE_ENABLED=1`.

**Pause and resume.** Set `RECONCILE_ENABLED` in the `Music Sync ENV` item to
`0` (pause) or `1` (resume), then `just sync-secrets` (CI also syncs on every
deploy). Verify with a `{"dry_run": false}` request: paused returns
`{"skipped": true}`. Capture keeps working while paused.

**Smart rules (the supported configuration path).** `just rules list` shows
every playlist's kind and rule. `just rules create "<name>" '<rule json>'`
creates a new private smart playlist; `just rules smart <id> '<rule json>'`
converts an owned playlist; `just rules curated <id>` classifies a new
playlist; `just rules clear <id>` turns a smart playlist back into a curated
one. Rules are JSON v1 over liked songs (`first_year`, `deezer_genres_any`,
`mb_tags_any`, `genre_any`, `in_playlist_ids_any`, `not_in_playlist_ids`,
`matches_rule_ids_any`, `not_matches_rule_ids`, `captured_by`, `liked_after`)
and reference playlists by stable ID. A predicate shared by several playlists
lives in one smart playlist's rule and the others reuse it, for example a
genre playlist `{"v": 1, "genre_any": {"deezer": ["Rap/Hip Hop"]}}` and year
playlists with `"not_matches_rule_ids": ["<genre playlist id>"]`; widening
the genre (say `"mb_tags_contain": ["rap", "hip hop"]`) is one `just rules
smart` change. The worker validates the
rule against the current catalog and writes the playlists row itself; agents
no longer write `playlists.rule`, `kind`, `pinned` or `expires_at` directly.

**Pending recovery.** A failed run leaves its remaining batches in
`music-sync/pending-reconcile.json.gz` (see Preservation and recovery). Rerun
the same operation: reconcile resumes its batches, metadata replay its
archive, and a rollout package resumes from its checkpoint when applied again
with the same digest. Long runs stop cleanly 50 minutes into a call and stay
pending; rerun the same operation to continue. The first run after a deploy with
an empty mirror cache (or the weekly rebuild) reads the whole catalog; later
runs read only what changed. Capture and new
baselines wait until it finishes. The one sanctioned exit is
`scripts/rollout.py observe --replan`, which replaces a pending observation-only
import (it never touched Spotify) with a fresh plan; it refuses any mutation,
replay or package checkpoint. Never delete the pending object to bypass recovery.

**Flags and review.** Run problems (unknown playlists, duplicate occurrences,
rule errors, missing IDs, errors) are batched into one open task per day of
identical text: a Soma task row when `flags_task_config` is set for the
workspace (`just workspace set <id>` with `FLAGS_TASK_CONFIG={...}`), otherwise
the Notion "Music Sync flags" Chore task through the dedicated integration.
Both are idempotent on retry. Unlike-while-curated review items become their own
quiet task rows (above). Observation imports return their flags to the operator
instead of filing them.

**Restore.** Every mutating flow archives the complete pre-write pull under
`raw/spotify-pull/` (captures under `raw/spotify-capture/`) through the
soma file API. To restore a playlist, read the retained pull for the
moment before the change and re-add its URIs with the `spotify` tooling; the
next reconcile mirrors the result. A rollout package records its pre-write
backup and post-write readback keys in its receipt, and the renamed
`<name> (pre-sync)` playlists are untouched rollback copies of the originals.

**Credential renewal.** Spotify refresh tokens expire after 180 days: re-mint
with `op run --env-file=.env.tpl -- uv run scripts/spotify_auth.py` (default
workspace) or send a new `just workspace connect-link <id>`. The hub token
(`music-sync-storage`), the dedicated Notion integration secret and the R2 token
are fields of `Music Sync ENV`; rotate the credential at its owner, update the
field, then `just sync-secrets`. The Modal CI token is re-minted by
`op-project-bootstrap` (`scripts/provision.py`).

**One-time rollout package.** `scripts/rollout.py observe` imports a complete
observation without Spotify writes. `build` turns a preview receipt, the owner's
decision manifest and a private spec (smart rules, classifications, selected
aliases) into an exact package: playlist renames, smart playlist creation,
occurrence edits, one-time likes and like normalization. `preview.py --package`
shows the first reconciliation after it. `apply --confirm <digest>` runs it in
the worker: every precondition is checked against a fresh complete pull before
the first write, progress is checkpointed, and Spotify is read back to verify.
Package likes count as Music Sync's own; its normalization unlikes are not owner
unlikes and create no review exceptions. `recognitions` imports past Shazam
recognitions create-only with explicitly estimated dates.

## Manual setup (the only steps that can't be codified)

1. **Spotify developer app** - create a dedicated "Music Sync" app at
   [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard),
   redirect URI `http://127.0.0.1:8080/callback`. Post-2026-02 apps in
   Development Mode get a reduced endpoint set (see spec section 7.1); save
   the client id/secret into the `Music Sync ENV` 1Password item.
2. **Spotify auth** - mint its own refresh token. The helper emits only a JSON
   object with `refresh_token` on stdout; pipe that to your credential store
   without logging it or writing it to a file. Consent instructions go to stderr:
   ```
   op run --env-file=.env.tpl -- uv run scripts/spotify_auth.py
   ```
   Spotify expires this refresh token every 180 days. A run that sees
   `invalid_grant` flags it in Notion and stops; re-mint with the command
   above. Use `--no-browser` for a remote browser and forward its loopback
   port 8080 to the machine running this command.

   App credentials are never shared with Derivations or a terminal client.
   Spotify Development Mode apps still share their developer account quota
   ([Spotify quota policy](https://developer.spotify.com/blog/2026-07-23-web-api-quota-updates)).
3. **Modal proxy-auth token for the legacy Shazam shortcut** - minted once in the
   Modal dashboard under Settings -> Proxy Auth Tokens, stored as `MODAL_KEY`
   / `MODAL_SECRET` in `iOS Shortcuts ENV`. The shortcut posts to `/capture`
   with those headers.
4. **Consumer capture enrollment** - `just clients issue "<device>"` and open
   the printed link on that device (or call `capture-access` with
   `{"action":"issue","label":"<device>"}` and enter the `capture-consumer`
   URL and token in the app). The app stores the token in Keychain; it never
   receives Modal or R2 credentials.
5. **`op-project-bootstrap`** - fills the `Music Sync ENV` 1Password item and
   mints the Modal CI token, both via `scripts/provision.py`:
   ```
   op-project-bootstrap ~/Desktop/coding/active-projects/music-sync/.env.tpl --repo alexjmiller5/music-sync
   ```
6. **Deploy** - push to `main`; CI reads the project's own Modal credential.

## Secrets

`.env.tpl` holds 1Password `op://` references only, pointing at the
`Music Sync` vault's `Music Sync ENV` item. Run everything through
`op run --env-file=.env.tpl -- <cmd>`. The hub token stored there
(`SOMA_HUB_TOKEN`) is named `music-sync-storage` on the hub, scoped to
`tables:read,tables:write` and read/write file grants for `raw/spotify-pull/`
and `raw/spotify-capture/`. Only this app writes the mirrored music catalog;
agents and the user write Spotify directly (see AGENTS.md).

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

## Rollout preview

Playlist predicates should reference stable IDs through `in_playlist_ids_any`
and `not_in_playlist_ids` lists. Renames do not affect those references;
descriptions use the current display names. Unknown IDs and ambiguous legacy
name references stop that rule for review. Duplicate inbox occurrences also
pause FIFO trimming and membership replacement until the keeper is reviewed;
they do not stop retaining genuine new recognition history.

Followed-artist source collection is independently read-only:

```sh
uv run scripts/followed_artists.py --output /private/state/followed-artists.json
```

It uses the existing Spotify grant, exhausts cursor pages and writes a new local
mode-0600 receipt. It never imports rows or follows/unfollows artists. Missing
scope produces an incomplete receipt; it never substitutes credentials or starts
enrollment. The receipt preserves full artist objects and collection times;
Spotify does not provide the dates when these follows began.

Run the owning operator command with its usual configuration (or `--remote`
through the deployed worker, optionally `--package <package.json>`):

```sh
uv run scripts/preview.py --output /private/state/music-preview.json
```

It always uses dry-run mode and creates a new mode-0600 local receipt. The
receipt includes full Spotify inputs, catalog rows and revisions, proposed
actions, quiet review exceptions, and exact curated-but-unliked migration
candidates. It never applies that candidate list, overwrites an old receipt,
or writes Spotify, the hub, archive objects, or tasks. Keep receipts outside
source control. Collection is paginated and not atomic; revisions and current
Spotify inputs must be checked again before any later apply.

Changed playlist observations retain create-only provenance references with
one original JSON Pointer per occurrence. Source dates and duplicate occurrences
stay in the raw archive; current membership rows and classification timestamps
do not replace them. Unchanged raw playlist contents do not emit new occurrence
references. This app never updates these insert-only evidence rows; the shared
service does not promise global append-only storage. Capture archives also keep
the accepted recognition request, capture UUID when supplied, market and server
receipt time. Receipt time is not an invented recognition time.
Recognition originals live under `raw/spotify-capture/events/`; their create-only
evidence points to `/event` in that original. Retry preserves the original bytes
and evidence timestamp, including after an ambiguous catalog insert. Legacy
operator requests without a capture UUID cannot distinguish retries from new
recognitions and should not be used for idempotent event delivery.

## Preservation and recovery

Cron, manual reconcile and capture synchronously dispatch to the same Modal
`worker`, configured with `max_containers=1` and
`@modal.concurrent(max_inputs=1)`. The entire read/archive/plan/apply cycle
runs there. FastAPI is a production dependency, including its real error
responses in the `--no-dev` image. Observation imports, rollout packages,
recognition imports and rule changes run in that same worker.

`run.reconcile(..., writes=False)` is an observation-only import: full owned
playlist pulls, actual liked values and complete observed membership rows.
It does not enforce auto-like, FIFO, rules or expiry. It retains newly detected
auto-like intent separately so an import cannot consume it. Unknown playlist
classification and duplicate membership aliases remain unresolved for review.

Every reconcile (including recovery) archives a fresh raw pull under
`raw/spotify-pull/`; captures archive the inbox before adding or trimming
under `raw/spotify-capture/`. Unique object names avoid overwriting backups.
Archive failures stop before Spotify mutation.

Before applying a plan, the worker stores its remaining batches and complete
planned actions in `music-sync/pending-reconcile.json.gz` in the project-owned R2
bucket. It checkpoints after each successful batch, stops at the first
Spotify, hub or checkpoint failure, and clears the object to JSON null only
when all batches finish. A new run resumes this plan before adopting a new
mirror baseline. Adds check live URI presence, so retries after uncertain
responses or partial 100-item client batches do not add duplicates. A like
attempt is checkpointed before the request: uncertain outcomes require positive
current evidence or review, never a blind retry that could reverse an unlike.
Hub patches merge by ID and transmit only present columns, preserving identity
and timestamps; explicit null remains an intentional update.

Curation state lives under `music-sync/curation/<workspace-hash>.json.gz`,
separate from catalog rows. It records the last full observation's raw archive
reference, liked/curated sets, confirmed app-origin likes, pending like intent,
and persistent unlike exceptions with before/after archive references. Its
next version is embedded in pending intent and committed only after all batches
succeed. Older mutating recovery plans are held for review because they may
contain removals from a superseded policy. Dry runs update none of this state.
`Hub.push` supplies one `updated_at` per invocation in UTC milliseconds when
absent, preserving any caller-supplied value. Spotify `added_at` and `liked_at`
are converted to UTC milliseconds ending in `Z` for the hub validator, including
saved recovery batches. Input rows and pending evidence remain unchanged;
JSON objects and arrays retain their values on the wire.

An incomplete mutating plan blocks capture and observation imports until
reconciliation recovers. A failed observation import can resume with
`writes=False`. Recovery finishes the saved intent first; Spotify edits made
after a failed run are reconciled on the subsequent fresh run. A persistently
failing operation requires operator attention; do not delete pending evidence
or advance the mirror to bypass it.

Recovery checkpoints, capture-client credential hashes and delivery receipts
use the project-owned `music-sync-state` R2 bucket.
Retained raw pulls and captures use the soma `/v1/files/` API with
scoped grants for `raw/spotify-pull/` and `raw/spotify-capture/`.

Recovery storage uses boto3 against `https://<R2_ACCOUNT_ID>.r2.cloudflarestorage.com`.
The token needs bucket object **read and write** permission, which Cloudflare
supports through the S3 API. `R2_ACCESS_KEY_ID` is the ID of the token stored
in `R2_API_TOKEN`; its SHA-256 hash is the S3 secret, derived only in memory.
Provisioning resolves the named token ID after minting the token value. For an
existing read/write token, populate its ID without reminting it. See
[Cloudflare's credential mapping](https://developers.cloudflare.com/r2/api/tokens/).
Only `NoSuchKey` means an absent object; bucket, permission, transport and
incomplete-read failures stop the flow. Keep `music-sync/` outside raw archive
lifecycle expiration, and reserve its pending object for this one worker/account.
The credential registry is `music-sync/capture-clients.json.gz`; delivery state
is stored below `music-sync/capture-receipts/<client-id>/<capture-id>.json.gz`.
Before capture it holds the selected Spotify track and canonical payload hash;
after success it holds the receipt ISRC. Tokens are independently random and
only their SHA-256 hashes are stored.

### Without 1Password

Plain env vars work everywhere `op run` is shown - export the fields listed
in `.env.tpl` instead. Mint the refresh token with
`uv run scripts/spotify_auth.py` using `SPOTIFY_CLIENT_ID` and
`SPOTIFY_CLIENT_SECRET` from the environment. Consume its `refresh_token` JSON
field directly into the credential store or process environment.

## Soma flag tasks

`FLAGS_TASK_CONFIG` optionally selects a Soma destination for flag tasks.
The same JSON object can be set as `flags_task_config` through the workspace
operator interface; JSON `null` restores the existing Notion destination. The
binding contains the catalog table, semantic column mapping, title, creation
defaults and open-status labels. Soma mode does not require Notion credentials.

The existing serialized worker owns flag receipts under
`music-sync/flag-tasks/` in Music Sync's recovery bucket. It uses the workspace's
scoped Soma client, not backing storage credentials. Flag/error text and the
day identify a notification batch, so repeated identical reports that day append
once. A unique open task with the configured title and project is adopted;
multiple candidates fail visibly. Closed tasks are preserved and a later new
batch creates a new task.

Before appending, the writer retains the target, full batch, marker and exact row
revision. Conditional patches preserve concurrent user edits. A lost response is
resolved by the retained marker; a newer row without that proof is reported as
ambiguous, never blindly appended again. New task IDs and their first payload are
stable across retries, including completed/deleted targets. Pending batches are
recovered even when the next reconcile produces no flags. Text is not truncated
to Notion's property limit. Observation and dry-run callers do not invoke the
writer.

Binding shape for the Soma `tasks` table:

```json
{"table": "tasks", "title": "Music Sync flags",
 "columns": {"title": "title", "status": "status", "notes": "notes", "due": "due_date",
             "priority": "priority", "tags": "tags"},
 "defaults": {"status": "To Do", "priority": "High", "tags": ["Chore"]},
 "open_statuses": ["To Do", "In Progress"]}
```

Review exceptions use the same binding, one create-only row per exception with
a stable ID. Neither path schedules a reminder or notification.
