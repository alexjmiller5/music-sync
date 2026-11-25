# Capture Speed Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Deliver accepted captures without loading unrelated catalog history, preserving capture outcomes and playlist policy.

**Architecture:** The event-driven drain uses the hub's existing bounded row-query API to fetch inbox/curated playlists, then catalog songs, memberships and observation evidence for the selected recording and live inbox recordings only. Existing serialized-worker capture and four daily full reconciliations retain their cache behavior. Older hubs keep the existing mirror path when session capabilities do not advertise bounded queries.

**Tech Stack:** Existing Python, httpx, pytest, Modal, Soma HTTP API.

**Spec:** Approved capture-speed design: immediate durable intake; scoped capture reads; unchanged identity, archive, outcome, cooldown, pending recovery, ordering and inbox-cap behavior. Healthy-provider 15-second acknowledgement is a measurement target, not a guarantee. Polling and full-sync cadence are unchanged.

## Global Constraints

- No provider credentials, personal data or runtime content in source.
- No new infrastructure or polling schedules.
- Never replay uncertain writes or advance a shared mirror cache outside its worker.
- Query pages are complete keyset walks, not an atomic snapshot.

## Review Focus

- Unavailable/older query capability keeps existing behavior; actual query errors stop before writes.
- Malformed, duplicate or repeated query pages fail closed.
- Unknown hand-added inbox songs retain existing non-eviction behavior.
- Deleted curated memberships still affect like-versus-curation chronology.
- Conflicting metadata and repeated capture outcomes retain existing evidence and duplicate protection.

### Task 1: Supported bounded row reads

**Files:** Modify `src/core/hub.py`; test `tests/test_hub.py`.

**Interfaces:** `Hub.supports_queries() -> bool`; `Hub.query(table, columns, filter=None) -> list[dict]`.

- [x] Add failing mocked-HTTP tests for session capability, authenticated query pages, inclusive projection, repeated/invalid cursors and duplicate identities.
- [x] Implement GET session capability and POST `/v1/rows/query` walks using existing idempotent read retry seam, limit 200, id ascending.
- [x] Verify focused tests and mutation-check a repeated-cursor guard.

### Task 2: Capture-specific mirror

**Files:** Modify `src/core/mirror.py`, `src/core/capture.py`, `AGENTS.md`; test `tests/test_capture.py`, `tests/test_mirror.py`.

**Interfaces:** `mirror.capture_playlists(hub) -> list[dict]`; `mirror.capture_mirror(hub, playlist_rows, isrcs) -> Mirror`. Shared row decoding preserves full mirror semantics.

- [x] Add failing end-to-end capture test whose query transport rejects unrelated songs/provenance or broad mirror loads and has a stale/absent cache. Verify no cache reads/writes, same add position/cap, retained archive/outcomes.
- [x] Cover partial queries failing before Spotify writes, deleted memberships, metadata evidence, no inbox and empty/large inbox identity chunks (at most 200 values per IN).
- [x] Decode scoped playlists/songs/memberships/evidence through the same mirror construction as the full path. Preserve equality-targeted recognition projection.
- [x] Select scoped path only outside the serialized cache-owning worker and only when capability is advertised. Preserve existing fallback and all four cron times.
- [ ] Commit/push and verify exact CI tests/deploy with gh watch. Offline suite/static checks and self-review pass.
- [x] Catalog-only live benchmark: 100 inbox recordings, 146 memberships, 721 observations, 31 HTTP 200 requests, context load 4.672 seconds, zero provider calls. Acceptance-to-acknowledgement and cold starts remain unmeasured.

## Execution notes

- Reuse the existing equality-filtered read batches for provenance; the newer bounded row-query API deliberately excludes evidence tables. No service/credential changes.
- Preserve deleted curation and existing metadata evidence. Both legacy and scoped capture paths run the same ordering, cap, unknown-member preservation, metadata and retry regressions.
- Added payload-free acknowledgement timing logs to measure delivery duration and receipt age on real captures.
- Initial regressions were red before implementation. Broad-mirror and repeated-cursor mutations were rejected, then restored.
- Resource admission remains one source process; final review performed inline without launching another agent.
