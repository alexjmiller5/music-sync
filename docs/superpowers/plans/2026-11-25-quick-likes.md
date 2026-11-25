# Guarded Quick Likes Implementation Plan

**Goal:** Detect recent positive saved-track events on the approved fifteen-minute schedule without inferring removals from a partial page.

**Architecture:** The existing serialized worker owns quick polling, a per-workspace baseline and a durable pending-event checkpoint. Inbox delivery reuses capture's fresh scoped context, archive, matching, ordering and outcome guard, with explicit like-source semantics that never create Shazam history. Four full-sync slots remain unchanged; other ticks poll only enabled workspaces.

**Constraints:** First page only, 50 items maximum; unchanged/baseline pages do no catalog/playlist writes. Validate unique identities, aware descending saved times and baseline overlap. Stop at cooldown or conflicting recovery. Retain unknown/added outcomes before maintenance; uncertain or known-added attempts without positive live membership stop for review. No additional credentials, infrastructure, policy changes or test likes.

- [x] Red tests for page validation/diff, first baseline, re-save, partial absence, no overlap, tied times and cooldown.
- [x] Implement one-page client, disabled workspace flag, and quick worker protocol including read-only validation.
- [x] Red integration tests for new like versus curated auto-like, no Shazam projection, archived evidence, unknown-result restart and checkpointed failures.
- [x] Implement explicit like observation/delivery and checkpoint recovery; block full sync on quick recovery.
- [x] Red cron tests for all four preserved full slots, quick ticks and stale work; implement same-slot quarter-hour dispatch.
- [ ] Full suite/static checks and mutation checks; inline review under single-process resource admission.
- [ ] Commit/push, verify exact CI, then save/retrieve exact deployed read-only call result before any replacement.
- [ ] Enable only after read-only validation passes; initialize baseline without replaying historical likes; verify settings/state/receipts. Natural-new-like timing remains an observed-use limitation.

One-minute polling was asked about during implementation. The approved fifteen-minute cadence remains unchanged; development quota is shared across apps under the same developer account, so extra keys are not a quota workaround.

## Verification record

- First semantic tests failed before implementation, including malformed windows, unknown recovery, separate like source, activation gates, CLI dry-run, and stale dispatch.
- Red recovery regression: an already-written like timestamp could hide unfinished membership/provenance. Retained attempted outcomes now finish maintenance instead of taking the already-observed shortcut.
- Red scheduler regression: a small cold-start delay could miss a full slot. Dispatch classifies the latest quarter-hour tick.
- Mutations removing window overlap, unknown-result presence guard and zero-retry behavior were rejected and restored.
- Native inline review preserves the existing single-source-process resource admission; no additional worker sessions.
