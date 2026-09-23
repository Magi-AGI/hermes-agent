# Upstream sync map — fork (live `1b8cc8c1b5`) onto stable v0.21.4 (`v2026.9.21`) — rev 2

Pull-only (Lake). Target = latest stable release. Plan phase 3 (see HARDENING-PLAN-REMAINING-2026-09-22.md).
Rev 2 folds in the triad review of rev 1 (Codex: approve with notes; Antigravity: approve).
Large test runs wait for phase-1 (test runner) approval; mapping, supersession checks and porting proceed.

## Size
- Merge-base `9a84bee265` (2026-09-06). Upstream to absorb: 7,850 commits, 7,763 files.
- Fork delta: 52 non-merge commits, 109 files, +14,722 / -413. Both sides touched 75 files.
- Trial merge (`git merge-tree`, no files touched): 32 conflicted files.

## Method
Branch `magi/sync-v2026.9.21` from the tag. Re-apply the fork delta as reviewed BUCKETS (not a history
merge). Per bucket: (1) supersession check against v2026.9.21; (2) port; (3) tests; (4) triad; (5) next.

### Test infrastructure on BOTH sides (Codex note)
The runner derives REPO from its own location, so each checkout must carry its own copy. Bucket 0 puts the
reviewed runner + guard extension onto:
- the sync branch (`magi/sync-v2026.9.21`), and
- a clean comparison branch `magi/tag-baseline-v2026.9.21` = tag + the same bucket-0 commits only.
A/B runs execute each checkout's OWN `scripts/run_tests_isolated.py` from that checkout's root, and every
test report records both revisions (sync HEAD and baseline HEAD). Upstream's conftest already carries the
Windows-root fix (#112692) and the gateway-spawn guard (`ca16cafee4`); bucket 0 ports only our additive
HERMES_TEST_LIVE_ROOTS deny roots onto upstream's conftest/hermes_state, adapting to their current shape.

### Exclusions (net-zero or withdrawn) — explicit manifest
- auth.json: 325aef31c0, cd865da18e (withdrawn; redesigned after the sync on upstream's shared writer
  `2be8e6147a`, with an explicit regression for a save that fails after token rotation — Codex).
- plugin autocomplete: 9ef07c3e2f, b01221938a, 417fc520ea, 3c1d70816f (dropped).
- ccb1168f23 is MIXED: carry its ceiling post-write-size fix and the EOF nit; its windows_only auth-test
  markers go with the auth redesign.
- 1b8cc8c1b5 (auth reset) is implied by not carrying the auth commits.

## Buckets
### 0. Test infrastructure (first)
Runner (`scripts/run_tests_isolated.py`, 3 review rounds) + HERMES_TEST_LIVE_ROOTS guard extension, onto
the sync branch and the baseline branch.

### A. Prior-sync buckets (09-06)
| bucket | commit | conflicted files |
|---|---|---|
| A1 code-0 classification + RelayBuffer | 2cb6b45546 | none |
| A2 local STT + web_server lifespan prewarm | ea7e42d66e | tools/transcription_local.py, tests/hermes_cli/test_web_server.py |
| A3 subscription-only compaction route guard | d33fec0394 | agent/auxiliary_client.py (131 upstream commits), agent/anthropic_credentials.py |
| A4 desktop session windows (+ e5b7b1d336, 7f39779443, 82972e687a from B) | d904f897e3 | electron/main.ts (116 upstream), preload.ts, session-windows.ts, window-state.ts, app/chat/index.tsx, use-desktop-integrations.ts, global.d.ts, i18n/en.ts |
Supersession questions: A1 vs upstream compute-host/relay changes; A3 vs upstream credential/routing
rework (2be8e6147a et al.); A4 vs upstream window-state changes (tray mode, work-area clamping, quit
finalization).

### B. 09-07/08 fixes (window fixes deferred to A4)
| commit | what | conflicted files / note |
|---|---|---|
| 48bb63b8d6 | skill anti-bloat (background_review "selective", ceiling guard, curator preview) | background_review.py, curator.py, skill_manager_tool.py, skill_provenance.py, tests/agent/test_curator.py; check upstream e333113871 (review origin/attendedness) and a79d1d3a71 (one-shot runs drop self-improvement) |
| 5a41e8aa96 | session-ownership phase-0 telemetry | hermes_cli/active_sessions.py — goes with C |
| ca86781a91 | bounded model-cache prefetch wait | PARTLY SUPERSEDED: upstream cdacc2bcf1 (model-options read path never waits on catalog probes) and cb24121a8e (/model listing cache-only). Keep upstream's behaviour; assess only the remaining callers of the prefetch helper, which still has unbounded waits, before carrying anything |
| 82972e687a, e5b7b1d336, 7f39779443 | window fixes | moved to A4 (same files) |

### C. Session ownership — 19 commits (range `aa06b3969b^..6afa0cecac`)
aa06b3969b 07a5b51a4d f68e11197b e2e1950ea0 9d13c3b2ff 40867caba9 de7d254499 a9e0e27660 eaf99848f4
17d5a5063d 018c7f41cf ee95dfac54 550ca12318 9ed40e8b6d ea9ca0e6fb 6afaaa9a18 d83c13fe7f 928c5b6c5f 6afa0cecac
(+ 5a41e8aa96 telemetry). Conflicts: tui_gateway/compute_host.py (14 fork commits), compute_host_bridge.py,
session_lifecycle.py (30 upstream), methods_slash.py, hermes_cli/active_sessions.py,
tests/hermes_cli/test_active_sessions.py.
HIGHEST RISK. Upstream's own ownership/lease fixes after 09-06: b397349065, ecefbfa6e1, baeebe4a60,
77915e344c. The WRITTEN supersession analysis starts EARLY (in parallel with A/B/D); implementation lands last.

### D. Group 1 (no auth) — the soaking candidate
| change | commits | conflicted files |
|---|---|---|
| MCP schema-cache TTL | 5660feeeaa, 2b2a723fab | none |
| orphaned tool futures + gate-hole skip | a40edf89cd, a70a0f6930, cec9d599c4 | none; the test moves to tests/agent/test_orphaned_tool_futures.py (upstream d10bb2ab6f moved tests/run_agent/ to tests/agent/ to mirror the source tree — this explains the merge-tree anomaly) |
| skills prompt | 109bc9502b | agent/prompt_builder.py (25 upstream, incl. 2dcebe6471 scratch dir, a79d1d3a71) |
| foreground ceiling + decoupling | 7995483c78, b422163210, ceiling part of ccb1168f23 | tools/skill_provenance.py |
| system-turn display + restated request + legacy/carrier/multimodal | 8896b1749f, a9ec6c8fe2, f86d1d3ea0, f2b8724680, b59445f1f2 | session_persistence.py, session_notifications.py, hydration.ts, types/hermes.ts |
| submit trace | 57f8c95f3b | use-composer-submit.ts |
| MCP non-interactive reconnect | 90108d52d3 | tools/mcp_oauth.py, tools/mcp_tool_server_run.py |

## Already provided by upstream (arrives with the tag — do not port)
Item 7 paging (49e6d661a0, 43bd5db429); MCP discovery lock budget (ef50bb344c, d93a48e694); cold-load
OAuth refresh (20d80bb36f); shared credential writer (2be8e6147a); test isolation fixes (#112692, ca16cafee4);
model-options/listing never wait on probes (cdacc2bcf1, cb24121a8e).

## Order
0 → A1, A2 → B (48bb63b8d6; ca86781a91 residual only) → D → A3 → A4 (+ window fixes) → C (analysis
started early) → phase 2b auth redesign → Group 2. Each bucket: triad before the next.
