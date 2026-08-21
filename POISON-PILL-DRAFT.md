# Poison-Pill Defusal — Design Draft (v2, upstream-adopt)

**Branch:** `magi/reconcile-2026-08-13` @ `1ffccb791d`
**Author:** Claude (orchestrator), for Codex + Antigravity review
**Status:** DESIGN DRAFT ONLY — no code committed. Feasibility dry-run done on a throwaway scratch branch (aborted, tree clean).
**Direction (user, 2026-08-19):** cherry-pick/port the upstream fixes now; defer the full rebase.

---

## 0. What changed since v1: Nous already fixed most of this

Fetched `upstream/main` = `b44871db0a` — **1856 commits ahead** of our reconcile base `590d547b4` (Aug 12). Several directly target our crash:

| Upstream commit | What it does | Relevance |
|---|---|---|
| **`577093def2`** perf(desktop): small tail page + backfill (Teknium, Aug 16) | Replaces the fixed **500-row** hydration (`getLatestSessionMessages`) with a **120-row** newest-first tail page + lazy "Show earlier" backfill (`getOlderSessionMessages`, new `transcript-tail`/`transcript-backfill` stores). Client-only. | **THE fix** for the reopen OOM. ~4× fewer rows/load. |
| `6e929a9694` + `f1025b2c00` + `5e2be43fd4` (turn-lease family) | Cross-process turn leasing: serialize turns, fence transcript writes to the lease holder | Upstream answer to hardening **front #2** (write-contention "storage was busy") |
| `b454e4da76`, `1db4801063`/`39e480c051`, `0d00ebef79`, `7a390f7c43`, `ab7f48d4b0` | SessionDB handle-leak fixes + context-manager protocol + bounded repair loop | Reduces per-backend memory bloat (N backends × 2.85 GB DB) |
| `4beca7a943`, `ee616b4b64` | memory-aware / capped concurrency | The Fix-B pattern, already upstream |

**Not found upstream:** a specific fix for the **crash-respawn re-fetch storm** (uncoordinated reconnect thundering-herd). Reconnect *hardening* exists (`ace85d63de`, `c23605ef76`, `2b7f49673f`) but not cross-window storm coordination. 120-row loads make the storm far less lethal, so this may no longer need a dedicated fix.

---

## 1. The crash (unchanged from v1, abbreviated)

Backend `MemoryError` at `hermes_state.py:8790 cursor.fetchall()`, via the display endpoint `get_session_messages` (`sessions.py:639`), fired as an **ExceptionGroup** (many concurrent loads in one process) → backend crash-looped ~15× → a renderer also OOM'd. Root = **500 fat rows (`SELECT *`, all 23 cols incl. 108 MB+ of never-displayed blobs) × many concurrent loads × a 2.85 GB `claudetriad` state.db**. The endpoint is already 500-row-capped; the naive full-transcript load was fixed earlier.

---

## 2. Plan — port the upstream fix + one additive backend win

### Part 1 — PORT `577093def2` (tail-page hydration) [primary crash fix]

**Feasibility (dry-run measured):** it's a **manual port, not a clean cherry-pick** — 6 conflict hunks total across 4 files, from 1856 commits of drift. Breakdown:
- **3 new files apply clean:** `store/transcript-tail.ts`, `app/chat/transcript-backfill.ts` (+ `.test.ts`, 285 lines).
- **2 files clean-modified:** `app/contrib/wiring.tsx`, `hermes.test.ts`.
- **4 conflicts, mostly mechanical upstream-drift** (our side == base `590d547b4`, untouched by our overlay — so the conflict is only the commit's newer context lines): `app/chat/index.tsx` (1 hunk), `app/contrib/hooks/use-background-sync.ts` (2), `hermes.ts` (1).
- **1 genuine 3-way conflict:** `app/session/hooks/use-session-actions/index.ts` (2 hunks) — the ONLY file both our Bucket B (`c3a25090f1`, session-titles) *and* the tail-page commit changed. Needs careful merge (keep our title/header wiring + adopt the 120-row `getLatestSessionMessages` change).

**Backend contract already matches** — verified our deployed endpoint returns `pagination: {limit, offset, order, returned}` and `get_messages` supports `latest`+`offset` back-from-newest paging + `include_inactive`/includeCompacted. The tail-page client detects truncation via "page came back full" (`returned == limit`) and the presence of the `pagination` field. **No backend change needed for Part 1.**

### Part 2 — Fix A: slim column projection on the display endpoint [additive backend win, complementary]

Upstream's tail-page fetches *fewer* rows but still full (fat) rows — it does **not** do column projection. So our Fix A still adds value: drop the ~108 MB of never-displayed blobs (`codex_reasoning_items` 93 MB, `codex_message_items` 15 MB, `api_content`) from the one display query.

- Add opt-in `columns: Optional[Sequence[str]] = None` to `get_messages` (`hermes_state.py:8741`); None = today's `SELECT *` (full back-compat). Used **only** from the display `_read()` (`sessions.py:630`).
- Guard the row→dict post-processing (`_decode_content` :8796, `tool_calls` parse :8798, `_decode_display_metadata` :8804) for absent projected columns.
- **Hard guardrail:** do NOT narrow `get_messages` globally — export (`sessions.py:754`) and portability (`hermes_state_portability.py:271/302`) need the full set. Resume/replay is already isolated via `get_messages_as_conversation` (`:9003`, own `_CONVERSATION_ROW_COLUMNS`).
- Display-needed cols: `id, role, content, tool_call_id, tool_calls, tool_name, timestamp, reasoning, reasoning_content, reasoning_details, display_kind, display_metadata`.

### Part 3 — Fix B (concurrency cap): DEMOTE to optional / conditional

With 120-row tail pages (Part 1) + lighter rows (Part 2), each load drops from ~500 fat rows to ~120 slim rows — roughly an order of magnitude less per load. That likely removes the OOM headroom problem on its own. **Recommendation: implement Parts 1+2, re-measure, and only add a concurrency cap if a burst still threatens the backend.** If needed, prefer the backend-side `asyncio.Semaphore` on the display endpoint (pending Q1 topology confirm).

---

## 3. Deferred (separate assessment, NOT this change)

- **SessionDB leak-fix + turn-lease families** — high value (front #2 write-contention + per-backend bloat) but they're **chained upstream commits** (`ab7f48d4b0` context-manager protocol is a likely dependency of the leak fixes; the turn-lease family is 5+ interdependent commits). Cherry-pick feasibility NOT yet assessed — likely needs its own port pass or argues for the full rebase. Treat as a follow-on decision after the crash fix lands.
- **Crash-respawn reconnect-storm coordination** — probably unnecessary once loads are 120 slim rows; revisit only if the loop recurs.
- **Full rebase onto `b44871db0a`** — the user chose cherry-pick-now over this; it remains the eventual clean-overlay goal (RECONCILIATION-PLAN.md).
- **DB maintenance / FTS trigram relocation / archive giant sessions** — the 2.85 GB root; separate operational workstream.

---

## 4. Open questions for the triad

- **Q1:** For a possible Part-3 concurrency cap — is `GET /api/sessions/{id}/messages` served by a single **per-profile** web backend (ExceptionGroup evidence says yes) or per-session? Determines backend-semaphore vs main-process limiter. Only blocking IF we do Part 3.
- **Q2:** Part-1 port — resolve the `use-session-actions/index.ts` 3-way conflict by keeping our Bucket-B title/header wiring and layering the 120-row change on top. Any concern with that ordering? (I'll show the resolved hunk for review before committing.)
- **Q3:** Part-2 projection param shape — `columns=` param on `get_messages` vs a dedicated `get_display_messages()`? (Leaning param, None-default.)
- **Q4:** Sequencing — land Part 1 (tail-page) alone first, verify it defuses the crash, THEN add Part 2 (projection) as defense-in-depth? Or ship 1+2 together? (Recommend 1 first — it's the proven upstream fix and independently sufficient-ish.)
- **Q5:** Part-1 is a **desktop repack** (TS changes → `npm ci` + electron-builder). Part-2 alone would be Python-only. Confirms this deploy needs the full desktop pack — acceptable, or do we want Part-2-first as a cheap Python-only interim while Part-1 goes through review?

---

## 5. Test + deploy

- **Part 1:** the commit ships its own `transcript-backfill.test.ts` (285 lines) + `hermes.test.ts` additions — port those too; run vitest `--project {electron,ui}`. Full desktop typecheck (3 tsconfigs) green.
- **Part 2:** unit-test `get_messages(columns=...)` projected keys + post-processing intact; regression-guard export/portability full-set. Targeted `-k "get_messages or session_messages or export"` fast gate, full `tests/` A/B at end.
- **Deploy:** Part 1 ⇒ desktop repack (native `npm ci` at root w/ node ≥22.22 + electron-builder, per the banked DESKTOP GATE MECHANICS). Part 2 ⇒ Python venv files only. Bundle both into one deploy once verified.
