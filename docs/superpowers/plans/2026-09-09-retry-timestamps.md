# Durable retry timestamps implementation plan

**Goal:** Persist edit timestamps before any reconciliation mutation.

**Architecture:** Prepare copied hub operation rows in `actions.apply` after
the dry-run return and before the existing checkpoint. The checkpoint in
`run.reconcile` already serializes operations, so its format stays intact.
Keep `Hub.push` as the direct-call timestamp and date-normalization seam.

**Tech stack:** Python stdlib, existing pytest and httpx mock transport.

**Spec:** `docs/superpowers/specs/2026-09-09-retry-timestamps.md`

Execution is inline, without subagents, under the approved scope.

- [ ] Run baseline `uv run pytest -q` and trace all apply/push callers.
- [ ] Add failing end-to-end regressions in `tests/test_release_regressions.py`:
  serialize pending intent through the real run checkpoint, interrupt a hub
  response or the first checkpoint, advance time, restart with a fresh Hub,
  and prove replay does not overwrite a newer edit. Include unstamped legacy
  intent, observation mode, and tombstones.
- [ ] Add focused coverage for supplied timestamps/null, unchanged caller
  payloads, dry-run non-stamping, and capture/migration fallback behavior.
- [ ] In `src/core/actions.py`, prepare copied operations with
  `{"updated_at": stamp, **row}` for hub rows before `checkpoint(ops)`.
- [ ] Run regressions GREEN. Mutate timestamp preservation and initial
  checkpoint ordering separately; verify restart and checkpoint tests fail,
  then restore the implementation.
- [ ] Update `AGENTS.md` with the durable timestamp contract, run the full
  suite plus `uv run ruff check .` and `uv run ruff format --check .`, and
  self-review the diff for scope, data and retry correctness.
- [ ] Commit safe work without pushing and report RED/GREEN, mutation checks,
  coverage, changed files, SHA and concerns to the assigned report path.
