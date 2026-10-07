# Curation policy implementation plan

**Goal:** Preserve intentional curation and recognition history while resolving release aliases before discovery, liking and smart routing.

**Architecture:** Keep planning pure. Persist review exceptions and reviewed keeper choices as workspace operational state through the existing archive abstraction. Retained originals hold occurrence history; catalog projections never replace that evidence. Existing serialized execution and pending-operation recovery order mutations.

**Execution:** Inline, test first. Product behavior has been approved; deployment and live enforcement remain separately gated.

## Constraints

- No personal selections, source observations or credentials in git.
- Curated membership is independent of likes. Explicit unlike creates a durable review exception only when formerly both liked and curated and still curated.
- Newly curated unliked recordings auto-like unless held by an explicit-unlike exception. A one-time preparation option includes existing curated recordings.
- Album-first selection is restricted to known equivalence or reviewed mappings. Significant duration/identity anomalies require review. Different performances are preserved.
- Album normalization verifies a replacement like before removing the superseded save. Preserve original like/add history and distinguish normalization from an explicit unlike.
- Repeated recognition retains another dated event, not another playlist occurrence. Retry of the same capture retains one event.
- Dry previews perform no writes. Full fresh observations precede enforcement previews.

## Tasks

1. Add regression tests for curated-unlike preservation, deduplicated review state, later-add suppression and baseline unliked behavior. Implement review actions before dependent mutations and persist through existing recovery checkpoints. Remove obsolete automatic curated undo behavior. Run focused tests and suite.
2. Add release-selection tests for album preference, unknown/different recordings, duration anomalies and explicit keeper overrides. Add parsed release metadata and apply selection to inbox/playlist/like plans without discarding source IDs or occurrence dates. Add verified replacement-like execution and interrupted-retry tests.
3. Add capture tests for already-liked/curated songs, repeated-recognition history and retries. Archive source payload, recognition/receipt timestamps and deterministic occurrence locators; expose history projections through the supported catalog contract. No title-only merging.
4. Fix review delivery to target a configured existing task by stable ID; retain all text without truncation and deduplicate repeated findings. Provide supported operator policy/exception interfaces and document configuration.
5. Prepare review manifests from retained evidence outside git, run full tests/static checks and audit changes. Commit and push the feature branch (no deploy trigger). Prepare fresh supported dry-run evidence; report remaining human-only recording decisions and deployment gate.

## Review focus

Incomplete observations never imply explicit unlike. Saved normalization does not look like a user unlike. Failed replacement verification never removes an existing saved version. Raw timestamps retain their precision/source. No-op repeat runs preserve metadata and produce no duplicate history or review items.

## Progress

- Baseline: 448 tests passed on current main.

- Curated independence and durable review regression tests pass; configured review delivery preserves long evidence. Suite: 457 passed; ruff check clean.

- Release selection and duplicate review are assigned elsewhere. Unfinished local
  selection work is preserved outside the active branch for owner reconciliation;
  it is not part of this release candidate. No duplicate decisions are repeated.
- Capture now retains one event per client/capture ID and reuses it on retries;
  event timestamps remain in retained originals, with create-only provenance
  references. Source recognition time is optional and never inferred from arrival.
- Review delivery destination is still a preference awaiting activation approval.
  No destination is configured or cut over by this branch. Existing optional
  Notion delivery support does not choose a destination for the user.
- Historical event backfill, catalog projections, replacement decisions and the
  full fresh integration preview remain rollout preparation, not completed work.
