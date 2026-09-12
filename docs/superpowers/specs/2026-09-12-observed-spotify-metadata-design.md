# Preserve observed Spotify metadata

**Date:** 2026-09-12
**Status:** approved for implementation on 2026-09-12.
**Rollout:** deployment, live catalog migration and backfill execution require
a separate operational handoff.
**Amends:** the Spotify metadata ownership and recovery portions of
[the redesign spec](2026-09-08-music-sync-redesign-design.md), especially
sections 2-4. Other behavior remains unchanged.

## Decision

Music Sync shall preserve Spotify metadata when it observes a recording.
Derivations shall enrich that record, never reconstruct its base fields.
Cataloging a song shall not depend on another provider finding it again.

The implementation shall use the existing importer, serialized worker,
catalog, provenance and raw archive interfaces. No new service, identity
scheme or generic enrichment framework is needed.

## Field ownership

| Fields | Writer and meaning |
| --- | --- |
| `title`, `artists`, `album`, `album_year`, `duration_ms` | Music Sync, from actual Spotify track observations. `album_year` describes the observed release, not a guessed original release year. |
| `spotify_ids`, `spotify_playable` | Music Sync, from observed identity and availability evidence. Search absence is not evidence of deletion or unavailability. |
| `deezer_genres`, `deezer_year` | Derivations, from Deezer source facts. |
| `mb_tags`, `mb_first_year` | Derivations, from MusicBrainz source facts. |
| `first_year` | Derivations, the existing minimum of known `album_year`, `deezer_year` and `mb_first_year`; never written back into `album_year`. |

The seven Spotify base columns shall no longer declare `http:spotify_isrc`
as their derivation. Types, required fields and ISRC row identity remain.
Enrichment shall not write Spotify base fields, likes, memberships or
capture history. Successful enrichment may revise its own source-specific
facts; "enrich" does not mean freezing an earlier incorrect derived value.
No LLM shall supply these facts.

## Observation handling

1. Observation-only import, enforcing reconciliation and Shazam capture
   shall use the same normalization and merge behavior. New rows shall
   include observed base metadata in their first catalog write. Existing
   rows shall receive observed metadata patches independently of enrichment.
2. Raw archives shall preserve every field actually received from Spotify.
   Capture shall retain the resolved track response as well as the pre-write
   inbox snapshot. Playlist reads shall request the existing normalized
   fields plus duration and relinking identity; no provider-wide field audit
   or extra metadata crawl is part of this change.
3. Missing, null or empty metadata shall not erase known nonempty values.
   A missing `is_playable` shall stay unknown, not become false. Explicit
   false shall be retained as evidence about that track in that market,
   not treated as proof that every alias of the recording is unavailable.
4. `spotify_ids` shall retain a deduplicated union of known and newly
   observed IDs, including `linked_from.id` when present. An empty search
   result shall never clear the list. Unverified historical aliases shall
   not be presented as verified playable replacements.
5. Display metadata shall prefer the existing representative track when it
   is observed again; otherwise prefer an explicitly playable observation,
   breaking ties by track ID. A profile change shall take fields from one
   track response. Album name and year shall move together only when both
   are present; otherwise retain the previous pair and its source attribution.
   When neither album field is known yet, preserve either supplied half without
   inventing the other. Do not combine retained and newly observed halves.
   Other absent fields shall retain their previous values and attribution.
   Existing raw archives preserve all alternate observations.
6. Metadata provenance shall identify the source archive, track and time
   of observation using the existing provenance interface. A newer direct
   observation may update a base fact; earlier observations remain in the
   archive/history. Observation time is the pull time, not playlist `added_at`.
7. Known live or membership track IDs shall remain usable without an ISRC
   search succeeding. Replacement selection still requires positive
   availability evidence. Unavailable songs shall remain catalog records.

## Enrichment and failures

Music Sync's enrichment backfill shall request only the Deezer,
MusicBrainz and `first_year` groups. It shall not reconstruct Spotify base
metadata with `spotify_isrc`. The general Derivations endpoint need not be
removed or changed for unrelated consumers.

A timeout, quota response, malformed payload or no-match shall not clear
previously known enrichment facts or base metadata. Lookup outcome and
field population are separate: a completed no-match does not mean metadata
was found. Confirmed source changes may make `first_year` stale; a failed
attempt shall not be counted as a successful source update.

## Safe recovery and cutover

Before changing live ownership, retain the catalog definitions and current
rows through existing backup interfaces. Drain affected in-flight writes,
remove the seven old derivation bindings through the supported catalog
interface, and verify that an old in-flight derivation cannot commit after
that contract changes. Coordinate the application update with this cutover;
do not leave an old importer running against the new ownership contract.
Any needed capture interruption requires an explicit operational handoff.

Recover missing base metadata from retained Spotify observations through a
metadata-only Music Sync replay. Replay shall fill gaps and union observed
IDs without overwriting existing nonempty facts, likes, memberships,
`first_seen`, capture edges or newer observation provenance. Repeating a
replay shall produce no further data changes. Conflicting existing values
shall be reported for direct-observation refresh, not silently rewritten.
An archive without a recorded request market shall retain unknown market
attribution; replay shall not substitute the current Spotify market setting.

Existing search-derived values and their evidence shall not be deleted or
relabeled as observations. Rows without recoverable evidence shall remain
explicit gaps. Recovery reporting shall distinguish recovered, already
present, conflicting, missing-source and failed rows.

Cron activation, playlist conversion and broader raw MusicBrainz/Deezer
retention remain separate work. This change shall not enable
`RECONCILE_ENABLED` or restart a blanket provider backfill.

## Acceptance checks

- All three import paths save observed base fields even when enrichment is
  unavailable; capture archives its resolved track before mutating Spotify.
- Sparse observations and negative/error enrichment results preserve known
  values and IDs. Explicit false and unknown availability remain distinct.
- Relinked and duplicate IDs survive deduplication; unavailable tracks keep
  their metadata. Profile selection is deterministic and provenance honest.
- A newly imported song can use its known Spotify ID without a derivation.
- Catalog ownership rejects stale derivation writes after cutover; declared
  enrichment still works and cannot alter base fields or user gestures.
- Metadata replay is idempotent, reports gaps/conflicts, and never rolls back
  current likes or memberships. Dry runs perform no writes.
- `first_year` can change after improved source evidence without changing
  the observed `album_year`. Existing project regression checks still pass.
