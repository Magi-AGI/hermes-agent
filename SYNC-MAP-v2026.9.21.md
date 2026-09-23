# Upstream sync map — fork (live `1b8cc8c1b5`) onto stable v0.21.4 (`v2026.9.21`)

Pull-only (Lake). Target = latest stable release. Plan phase 3 (see HARDENING-PLAN-REMAINING-2026-09-22.md).
Large test runs wait for phase-1 (test runner) approval; mapping and porting proceed now (Codex).

## Size
- Merge-base `9a84bee265` (2026-09-06). Upstream to absorb: 7,850 commits, 7,763 files.
- Fork delta: 52 non-merge commits, 109 files, +14,722 / -413. Both sides touched 75 files.
- Trial merge (`git merge-tree`, no files touched): **32 conflicted files**.

## Method (same as the 09-06 reconcile)
Branch `magi/sync-v2026.9.21` from the tag. Re-apply the fork delta as reviewed BUCKETS (not a history
merge). Per bucket: (1) supersession check — does v2026.9.21 already fix it? if so drop/adapt;
(2) port; (3) tests via `scripts/run_tests_isolated.py`, A/B against a clean tag worktree so
pre-existing Windows failures are not blamed on the port; (4) triad; (5) next bucket.
Net-zero commit sets are NOT carried: auth.json (325aef31c0, cd865da18e — withdrawn; redesigned
separately after the sync with a rotation-failure regression, per Codex) and the four autocomplete
commits (9ef07c3e2f..3c1d70816f, dropped).

## Buckets and their conflicts
### A. Prior-sync buckets (09-06) — 4 commits
| bucket | commit | conflicted files |
|---|---|---|
| A1 code-0 classification + RelayBuffer | 2cb6b45546 | none |
| A2 local STT + web_server lifespan prewarm | ea7e42d66e | tools/transcription_local.py, tests/hermes_cli/test_web_server.py |
| A3 subscription-only compaction route guard | d33fec0394 | agent/auxiliary_client.py (131 upstream commits), agent/anthropic_credentials.py |
| A4 desktop session windows | d904f897e3 | electron/main.ts (116 upstream), preload.ts, session-windows.ts, window-state.ts, app/chat/index.tsx, use-desktop-integrations.ts, global.d.ts, i18n/en.ts |
Supersession questions: A1 vs upstream compute-host/relay changes; A3 vs upstream's credential/routing
rework; A4 vs upstream window-state changes (tray mode, work-area clamping, quit finalization).

### B. 09-07/08 fixes — 6 commits
| commit | what | conflicted files |
|---|---|---|
| 82972e687a | peer windows don't resume last session | use-desktop-integrations.ts |
| 48bb63b8d6 | skill anti-bloat (background_review "selective", ceiling guard, curator preview) | background_review.py, curator.py, skill_manager_tool.py, skill_provenance.py, tests/agent/test_curator.py |
| e5b7b1d336 | sequential readiness gating for window reopen | main.ts, preload.ts, session-windows.ts, global.d.ts, en.ts |
| 5a41e8aa96 | session-ownership phase-0 telemetry | hermes_cli/active_sessions.py |
| ca86781a91 | bounded model-cache prefetch wait | none (check upstream model_switch_providers) |
| 7f39779443 | bulk-close cascade doesn't shrink saved layout | main.ts |
Supersession: upstream `e333113871` (review origin/attendedness in skill_provenance), `a79d1d3a71`
(one-shot runs drop self-improvement) touch the anti-bloat area.

### C. Session ownership — 18 commits (09-10/11, 14 Codex rounds)
aa06b3969b .. 6afa0cecac. Conflicts: tui_gateway/compute_host.py (14 fork commits), compute_host_bridge.py,
session_lifecycle.py (30 upstream), methods_slash.py, hermes_cli/active_sessions.py, tests/hermes_cli/test_active_sessions.py.
HIGHEST RISK. Upstream shipped its own ownership/lease fixes after 09-06: b397349065 (isolated turns stop
fencing out the parent lease), ecefbfa6e1 (lease borrow inert across compression rotation), baeebe4a60
(hold the canonical lease until a closed session's isolated turn settles), 77915e344c (preserve active
isolated turns after disconnect). Step 1 for C is a written supersession analysis: which of our 14 rounds'
guarantees upstream now provides, which still need porting, and which conflict in design.

### D. Group 1 (no auth) — soaking candidate
| change | commits | conflicted files |
|---|---|---|
| MCP schema-cache TTL | 5660feeeaa, 2b2a723fab | none |
| orphaned tool futures + gate-hole skip | a40edf89cd, a70a0f6930, cec9d599c4 | none (see note) |
| skills prompt | 109bc9502b | agent/prompt_builder.py (25 upstream, incl. 2dcebe6471 scratch dir, a79d1d3a71) |
| foreground ceiling + decoupling | 7995483c78, b422163210, part of ccb1168f23 | tools/skill_provenance.py |
| system-turn display + restated request + legacy/carrier/multimodal | 8896b1749f, a9ec6c8fe2, f86d1d3ea0, f2b8724680, b59445f1f2 | session_persistence.py, session_notifications.py, hydration.ts, types/hermes.ts |
| submit trace | 57f8c95f3b | use-composer-submit.ts |
| MCP non-interactive reconnect | 90108d52d3 | tools/mcp_oauth.py, tools/mcp_tool_server_run.py |
Note: tests/agent/test_orphaned_tool_futures.py conflicts with no commits on either side since the
merge-base — likely an add/add or rename clash with an older file; investigate before porting D.

## Proposed order
A1, A2 (low conflict) → B (small) → D (recent, well-tested) → A3 → A4 + e5b7b1d336 + 7f39779443 together
(desktop windows) → C last, after its supersession analysis. Each bucket: triad before the next.
Then: auth redesign on the synced base (phase 2b), then Group 2.
