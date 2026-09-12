# Observed Spotify Metadata Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve Spotify observations directly and recover catalog gaps without reconstructing base metadata through enrichment.

**Architecture:** Reuse Music Sync's plain Python core, serialized worker, existing action checkpointing and retained-file API. A shared normalization/merge module owns Spotify metadata patches and provenance. Derivations stays a source-fact service; live ownership changes and replay execution are a separate rollout.

**Tech Stack:** Python 3.13+, existing uv/httpx/dataclasses/pytest/ruff; Modal only in app.py. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-12-observed-spotify-metadata-design.md`, amending `docs/superpowers/specs/2026-09-08-music-sync-redesign-design.md`.

## Global Constraints

- Music Sync shall preserve Spotify metadata when it observes a recording.
- Derivations shall enrich that record, never reconstruct its base fields.
- Cataloging a song shall not depend on another provider finding it again.
- Types, required fields and ISRC row identity remain.
- Missing, null or empty metadata shall not erase known nonempty values.
- Observation time is the pull time, not playlist `added_at`.
- Enrichment shall not write Spotify base fields, likes, memberships or capture history.
- This change shall not enable `RECONCILE_ENABLED` or restart a blanket provider backfill.
- No production data, credentials, deployment, catalog mutations or provider calls during implementation. Offline fixtures only; no personal data committed.
- Keep tests and code in their owning repositories; no runtime dependency on another working tree.

---

## File structure and task boundaries

Task 1 owns `src/core/metadata.py` and integrations in model, mirror, reconcile, run, capture and Spotify client. Task 2 adds metadata-only replay using those exact interfaces, with a thin branch in the existing authenticated reconcile endpoint. Task 3 removes Spotify reconstruction from the backfill, adds the ownership precondition and documents cutover. Task 4 is a separate Derivations-repo patch protecting source facts from empty/error outputs. Life Data's existing transaction/catalog guard is verified with its own offline tests; no music-specific schema belongs in that repo.

Each task starts from the current previous-task commit, runs focused red/green tests, self-reviews and commits. Run the owning repo's full suite and Ruff once before committing, not on every small edit. Independent review follows each task. Track operational items as pending, not completed by tests.

### Task 1: Preserve observations in all ingestion paths

**Files:**
- Create: `src/core/metadata.py`, `tests/test_metadata.py`
- Modify: `src/core/model.py`, `src/core/mirror.py`, `src/core/reconcile.py`, `src/core/run.py`, `src/core/capture.py`, `src/core/spotify_client.py`
- Test: `tests/test_reconcile.py`, `tests/test_capture.py`, `tests/test_run.py`, `tests/test_mirror.py`

**Interfaces:**
- Consumes: `Song`, `LiveItem`, `Mirror`, `Live`, `Action`; `item_from_raw(raw)`; action kinds `upsert_song` and `edge`; existing archive and pending-operation interfaces.
- Produces: `metadata.observation_actions(mirror: Mirror, live: Live, now: datetime, *, source_ref: str | None = None, market: str | None = None, fill_only: bool = False) -> list[Action]`.
- Add keyword-only `source_ref` and `market` to `reconcile.plan` and `observe` without breaking existing callers. Append defaulted fields to dataclasses to preserve positional constructor compatibility.
- Add `album`, `album_year`, `duration_ms` to Song and normalized observations, retain linked-from identity, and represent `LiveItem.playable` as `bool | None`. Carry raw track observations so liked ISRC deduplication does not lose alternate IDs. Read existing field-level observation provenance when needed for representative selection; use existing `takeout` / `evidence_of` vocabulary and archive keys, not new catalog options.

- [ ] **Step 1: Add the dropped-metadata regression before implementation.** In `tests/test_reconcile.py`, use the existing constructors and time fixture:

```python
def test_new_song_keeps_observed_metadata_without_derivation():
    live = Live({}, {"A": item("A", track_id="observed")}, {})
    actions = reconcile.plan(Mirror({}, {}, {}, [], set()), live, NOW)
    row = next(a.row for a in actions if a.kind == "upsert_song")
    assert row["title"] == "n"
    assert row["artists"] == ["a"]
    assert row["spotify_ids"] == ["observed"]
```

- [ ] **Step 2: Run `uv run pytest tests/test_reconcile.py -k new_song_keeps -q`.** Expected: missing `title` in the first song write.
- [ ] **Step 3: Implement shared observation planning and wire every ingestion path.** Use this merge rule for nonempty scalar/array facts, with the spec's coherent album-pair and representative selection applied before it:

```python
def present(value):
    return value is not None and value != "" and value != []

# Construct patches, never modify the input mirror or input response.
patch = {
    key: value for key, value in observed.items()
    if present(value) and value != existing.get(key)
    and (not fill_only or not present(existing.get(key)))
}
```

Union aliases even during fill-only recovery; keep display representative separate from choosing a currently known playable target. An observed unavailable alias must not downgrade a different playable alias. Unknown must not enter the unplayable-relink branch. Relinking requires a positively observed playable alternative, never a legacy unverified search alias. Existing live/membership IDs remain usable for normal routing and undo.

Coherent album replacement protects existing release facts. When both existing album fields are empty, retain either supplied initial half without inventing the other; never combine retained and newly observed halves. Store observation time inside provenance `detail.observed_at`, not a new top-level column.

Metadata actions must merge into the FIRST new-song write, not follow a skeletal insert. Reconciliation must include freshly observed/new songs in its effective rule view and choose IDs from that view; do not let this inclusion change unheart precedence. Store raw source_ref once per pull before planning, carry it into checkpointed provenance, and leave pending intent intact on failure. Dry runs may calculate a reference but must not create an archive or provenance rows externally.

Capture archives `resolved_track` alongside the pre-write inbox payload before Spotify mutation, normalizes both through the same helper, and saves metadata for already-known as well as new songs. Capture origin edges remain unchanged and distinct from field evidence. Preserve metadata for observed inbox items that are about to age out. Do not broaden this task into the unrelated capture retry/FIFO repair backlog.

- [ ] **Step 4: Add/run behavioral checks.** Extend the same test files with literal dummy responses for: both item/track envelopes; absent playability; explicit false; duration and linked_from; two IDs for one ISRC; missing metadata not erasing old values; stable representative and album pair; updated source attribution; first import into a `v=1` smart pool; existing-member ID fallback; Shazam archive content and order; both observation/enforcement paths; no enrichment requests. Run `uv run pytest tests/test_metadata.py tests/test_reconcile.py tests/test_capture.py tests/test_run.py tests/test_mirror.py -q`.
- [ ] **Step 5: Run full regression/static checks and commit.**

```sh
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
git add src/core tests
git commit -m "fix: preserve observed Spotify metadata during ingestion"
```

### Task 2: Add metadata-only recovery through the serialized worker

**Files:**
- Create: `src/core/metadata_replay.py`, `tests/test_metadata_replay.py`
- Modify: `app.py`, `tests/test_app.py`, `README.md`

**Interfaces:**
- Consumes: Task 1's `observation_actions(..., fill_only=True)` and existing `archive.get`, `load_mirror`, `actions.apply`.
- Produces: `metadata_replay.run(settings, archive_key: str, observed_at: str, *, dry_run: bool = True, hub=None) -> dict`.
- Existing reconcile endpoint accepts `{"metadata_replay":{"archive_key":"raw/spotify-pull/example.json.gz","observed_at":"2026-01-01T00:00:00.000Z"},"dry_run":true}` and dispatches to the same worker. A replay defaults to dry-run; applying requires the explicit JSON boolean `false`. It is observation-only and does not require enabling the enforcement cron.

- [ ] **Step 1: Write a failing replay test using real core behavior and in-memory archive/hub doubles.** The double updates rows on push, so repeating replay actually tests idempotency:

```python
def test_replay_only_fills_metadata_and_is_idempotent(settings, replay_env):
    from core import metadata_replay
    key, stamp, hub = replay_env
    before = {k: dict(v) for k, v in hub.songs.items()}
    out = metadata_replay.run(settings, key, stamp, dry_run=False, hub=hub)
    assert out["recovered"] == 1
    assert hub.songs["USAAA2600001"]["title"] == "Observed title"
    assert hub.songs["USAAA2600001"]["liked"] == before["USAAA2600001"]["liked"]
    calls = len(hub.pushes)
    metadata_replay.run(settings, key, stamp, dry_run=False, hub=hub)
    assert len(hub.pushes) == calls
```

Define `replay_env` in this test file with one existing song missing title, an archived track with that literal title and a conflicting historical like, plus real normalized archive envelopes. No external sockets.
- [ ] **Step 2: Run `uv run pytest tests/test_metadata_replay.py -q`.** Expected: missing replay module, then missing behavior once module exists.
- [ ] **Step 3: Implement a one-archive replay, not a new batch coordinator.** Allow only normalized object keys below `raw/spotify-pull/` and `raw/spotify-capture/`; reject traversal, other namespaces, malformed types/envelopes, naive/invalid dates and missing objects before writes. Accept retained reconcile and capture envelopes, including old captures without resolved_track. Parse UTC milliseconds. Never construct a Spotify client or call a provider.

```python
# The critical semantic boundary: only already-cataloged recordings.
actions = observation_actions(
    mirror, live, observed_time, source_ref=archive_key,
    market=settings.spotify_market, fill_only=True,
)
actions = [a for a in actions if a.isrc in mirror.songs]
```

Before any replay apply, reject pending reconciliation rather than overtaking it. Use the same existing pending operation mechanism for replay writes or an explicitly separate pending replay key in the same project store; checkpoint metadata/provenance intent before hub mutations, make recovery unable to be mistaken for enforcing reconciliation, and block capture/reconcile until that intent finishes. Prefer reusing current pending format when safe. Preserve edge provenance on a retry that follows a successful song patch.

Return counters `recovered`, `already_present`, `conflicting`, `missing_source`, `failed`, plus structured planned patches/errors. Row outcome categories are mutually exclusive, with conflict taking precedence over recovered when a row has both filled and conflicting fields; count confirmed applied fills separately if needed. Missing rows are reported, not created. Existing nonempty facts and old derivation provenance are not relabeled. Repeated successful replay makes no writes, including timestamp-only/provenance-only churn. Dry-run writes nothing.
- [ ] **Step 4: Run focused replay/endpoint tests.** Cover schema validation, pending state, partial hub failure/recovery, old and new archives, no provider/Spotify calls, false vs omitted/string false dry_run, conservative conflict reporting, preserved user state, and second-run no-op. Test the real endpoint response through existing FastAPI helpers.
- [ ] **Step 5: Verify and commit.**

```sh
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
git add app.py src/core tests README.md
git commit -m "feat: replay retained Spotify metadata without user-state changes"
```

### Task 3: Enforce the ownership boundary and remove Spotify reconstruction

**Files:**
- Modify: `src/core/hub.py`, `src/core/run.py`, `src/core/capture.py`, `src/core/metadata_replay.py`, `scripts/backfill_derive.py`, `tests/test_hub.py`, `tests/test_backfill_derive.py`, relevant ingestion tests, `README.md`, `AGENTS.md`
- Create: `tests/test_metadata_contract.py`, `docs/observed-metadata-rollout.md`

**Interfaces:**
- Produces: `Hub.catalog() -> dict` using existing `GET /v1/catalog`; `metadata.require_observed_contract(hub) -> None` validating all seven Spotify properties exist and have no derivation binding. Read-only dry-run planning remains available with the old contract; any mutating metadata path must fail before Spotify writes against the old contract.
- Existing `scripts/backfill_derive.py` public run/CLI remains, but allowed groups are only Deezer, MusicBrainz and first_year. Explicit `--col title` is rejected with a useful message, never silently routed.

- [ ] **Step 1: Add a failing contract test with a catalog double that keeps `songs.title` bound to Spotify search.**

```python
def test_old_derived_contract_refuses_mutating_import(old_catalog_hub):
    import pytest
    from core import metadata
    from core.hub import HubError
    with pytest.raises(HubError, match="title"):
        metadata.require_observed_contract(old_catalog_hub)
```

The double supplies all seven literal typed properties with just title still derived. Include missing-property and supported-new-contract cases. Tests verify checks happen before mutations, not just helper behavior.
- [ ] **Step 2: Run `uv run pytest tests/test_metadata_contract.py tests/test_backfill_derive.py -q`.** Confirm failures before production changes.
- [ ] **Step 3: Remove `title` from `SOURCES` and the Spotify-negative special case.** Preserve existing source/backoff behavior except refresh first_year only after confirmed upstream writes and after rereading current source values/proofs. Do not count failed/deferred rows as done or manufacture completion from missing response fields. Derivations returning no usable fields remain explicitly unresolved; this work does not invent a new negative-cache protocol.

```python
SOURCES = {
    "deezer_genres": ("deezer_isrc", ("deezer_genres", "deezer_year")),
    "mb_tags": ("musicbrainz_isrc", ("mb_tags", "mb_first_year")),
    "first_year": ("first_year", ("first_year",)),
}
```

Wire the ownership check into new mutation paths and pending replays. Continue to preserve preexisting pending intent, never manually clear it. Read catalog via supported interface, no direct D1 access. Implement a safe staged cutover description: backup via supported interfaces, drain affected writers, remove bindings with installed Life Data CLI, deploy verified code, refresh catalog docs, run metadata replay preview, approve/apply, verify, then leave cron disabled. Document deployment approval and any needed capture downtime as operational gates. Do not execute them.
- [ ] **Step 4: Verify the existing Life Data stale-catalog commit guard offline.** Use its `worker/test/derive.test.js` and real checked-commit implementation. Add a generic test in Life Data only if no existing test proves removal during an in-flight request aborts the write; no music table literals or new ownership framework there. Record exact command/evidence in the task report.
- [ ] **Step 5: Full tests/static checks; update current-code docs and commit.** AGENTS must say live cutover is required, not claim the remote catalog already changed. README explains observed album_year versus enriched first_year and truthful unresolved-source reporting.

```sh
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
git add src/core scripts tests README.md AGENTS.md docs
git commit -m "fix: restrict music enrichment to source-owned facts"
```

### Task 4: Protect source enrichment from empty and error results

**Owning repository:** Derivations, on its own isolated nondeploy branch. This task does not add Life Data knowledge to that service.

**Files:**
- Modify: `src/core/deezer.py`, `src/core/musicbrainz.py`, `src/core/years.py`, `app.py`, `AGENTS.md`
- Test: `tests/test_deezer.py`, `tests/test_musicbrainz.py`, `tests/test_years.py`, `tests/test_app.py`

**Interfaces:** Keep existing endpoint URLs, proxy auth, successful populated field names and `_source_ref`. Omit unobserved/null/empty enrichment fields instead of returning destructive clears. A no-match may return only `_source_ref`; Music Sync must report it as unresolved rather than a populated record. Source outage/quota/malformed responses remain structured errors, never no-match.

- [ ] **Step 1: Write failing non-erasure tests in the source test files.**

```python
def test_no_match_emits_no_destructive_enrichment_values():
    from core import deezer
    result = deezer.to_row("USAAA2600001", None, None, "2026-01-01")
    assert "deezer_genres" not in result
    assert "deezer_year" not in result
    assert result["_source_ref"]
```

Add corresponding MusicBrainz and all-unknown year cases; populated values must still return normally. Add HTTP-200 Deezer quota/error and malformed envelopes so they cannot be checkpointed as a successful empty source. Use existing upstream error class and endpoint error renderer; only the recognized genuine no-data case is no-match. Do not alter Spotify adapter behavior for other consumers.
- [ ] **Step 2: Run `uv run pytest tests/test_deezer.py tests/test_musicbrainz.py tests/test_years.py tests/test_app.py -q`.** Confirm expected semantic failures.
- [ ] **Step 3: Preserve source evidence by omitting missing facts.** Reuse existing functions, no new dependency or generic provider framework:

```python
return {key: value for key, value in result.items()
        if value is not None and value != [] and value != ""}
```

Apply only to music enrichment results; do not change TMDB semantics. Validate malformed source responses at the relevant boundary; recognize Deezer quota and preserve Retry-After through its existing upstream error interface. Retain MusicBrainz pacing. Existing-value preservation is achieved by the hub's present-key patch behavior, not by sending old rows to Derivations. Check that behavior offline.
- [ ] **Step 4: Run full owning-repo tests/Ruff and mutation-check the preservation guard.** Update endpoint docs to distinguish no data from failures and to state that missing fields are omitted. Record unresolved negative-cache semantics as an existing repair backlog item, not a completed lookup.
- [ ] **Step 5: Commit the scoped Derivations patch.**

```sh
git add src/core app.py tests AGENTS.md
git commit -m "fix: omit absent music enrichment instead of clearing known facts"
```

## Final verification and handoff

Review both complete branch diffs, using dummy end-to-end payloads through ingestion, hub-style sparse merge and replay. Run Music Sync and Derivations suites/static checks on final code. Confirm no new dependency, live write, enabled cron or provider scrape occurred. Push only nondeploy branches; do not merge or push main. Update the existing repair task with code evidence and the still-pending operational cutover/recovery. Hand off the exact deployment/catalog/replay gates without claiming the live backfill complete.
