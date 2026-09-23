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
test report records both revisions (sync HEAD and baseline HEAD). Comparisons against the OLD fork base
(6afa0cecac) also get the runner overlay first — never a direct pytest run with HERMES_HOME cleared
(Codex: that is not equivalent, and clean logs cannot prove containment). Upstream's conftest already carries the
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

## Progress (sync branch)
| bucket | commit | status |
|---|---|---|
| 0 | 86bc926241 | approved |
| A1 | 8fa5ab0dbf (re-port WITHOUT RelayBuffer, Lake's call) + a4115bb96f (raising rpc_sink fix, both reviewers) | APPROVED |
| A2 | d5fb8ebae4 | approved |
| B | 30d3fa1599 (48bb63b8d6) + 3982518f82 (curator preview bookkeeping, Codex #4-#6); ca86781a91 residual: afc54d5e6a reverted (75df8f6ce5), replaced by 9bcc841ff1 (model.save_key + recommended-default cache-only, bounded single-provider wait) | APPROVED |
| D | 42c5344382..5cafcfff1d (16 picks + sync adaptations; ccb1168f23 split: ceiling part 42398eca41, EOF nit in 0e1b43f781, auth markers deferred to 2b); follow-ups cc463233ed..753dabbc29, 56d7267d2b, 8a0f37b2b7, e7f5201c9a, 70e8ec6955, 8efbd2ec03 | APPROVED |
| C | DECIDED 2026-09-22 (Lake; Codex + Antigravity agree): adopt upstream's ownership model, do not port the 19 commits. G1-G4 are RELEASE GATES. Codex's bar: G2 = new id owned before any continuation write, failure stops the turn; G3 = takeover waits for / fences the old executor. Implementation in progress | in progress |
| C impl | 85a7b52b87..bc7d17986d (telemetry, G1, G4, G2/G3 design, scenarios) + fencing follow-ups 332888e317..33b6fe52b2, 69d1d3e9a4, 67017165b8, cc24078df1, 2b0723622a, 986a4a8441, 912e489a0a, 8711773970 (7 Codex rounds: compression commits lease-fenced; one effective ownership token; sticky loss; ACP metadata/transcript split; lease-loss interrupt parked on the committing thread's own fence) | APPROVED 2026-09-23 (Codex + Antigravity) |
| A3 | df65e59b80 + 214c3bb682; follow-ups 01f6d79802 (effective-auth screen), 2ce27e8d6a (native compaction), 7caa28fc2e (case-variant headers) | APPROVED 2026-09-23 (Codex + Antigravity) |
| A4 | 7a0d125cd4..a13a5e6578 (d904f897e3 + e5b7b1d336, 7f39779443, 82972e687a; no upstream equivalent) + 3f3bc1afb4 (per-window saved layout, peer windows persisted, legacy migration; Lake: fix criterion 2 now) + edbae6e276 (merge-not-rederive guard) + Codex round-1 fixes 6c28e0d231..625ca48ca4 + ff417e243e (Lake: closes NEVER remove; only Window > Close and Forget This Window, Ctrl+Alt+W) + Codex round-3 fixes f440ffb744..3aba541b03 (current-session readiness; Reopen never destroys a user window; Forget invalidates queued entries; one shared Reopen/Resume queue; launch route kept until boot establishes it; pending additions retained) + Codex round-4 fixes 72946364e2..deebbd85f6 (readiness+probe bound to the latest identity; existing windows probed on their current route; queue never re-admits) + 8028e0a902 (one complete readiness identity: main-assigned generation, attempt, session, profile, connection; Codex round 5) + a8ff04cec5, 1baba0e565 (round 6: pending before route resolution; per-document readiness tokens; main-frame document lifecycle only) | APPROVED 2026-09-23 with notes (Codex: no blockers; Antigravity: approve); residuals below; criteria 3-4 need Lake's live check |
| extra | 8e188195ef git probes GIT_OPTIONAL_LOCKS=0; b7c9d156af compute-host fallback AttributeError; 01f6d79802 side-find: route-guard test file was hanging (0 tests) | APPROVED |

## Open items
- A4 residual R-A4-1 (Codex r6, non-blocker): a reload can let Reopen advance before the replacement document commits (old document's probe completes during navigation; the old document stays valid until commit by design). No saved-window loss. Future fix: track navigation-in-progress and hold admission until commit/cancel (session-reopen-gate.ts ~219).
- A4 residual R-A4-2 (Codex r6, non-blocker): a missing/null report token passes validation when no document is current (null == null). No ordinary production path (preload always stamps a numeric token). Future fix: require a non-null current token and a matching numeric report token (session-reopen-gate.ts ~190).
- A4 live check (Lake): criteria 3-4 + round 3-6 additions (see A4 reports); back up %APPDATA%\Hermes\session-windows-state.json first.
- Fallback-session full-turn stall (compute host, upstream): not diagnosed.
- Yuanbao recall redaction: whole-transcript rewrite, unfenced (see BUCKET-C doc); Lake does not use the adapter.
- Next: A4 (desktop session windows + e5b7b1d336, 7f39779443, 82972e687a), then phase 2b auth redesign, Group 2, Pass A remainder, Pass B.

## Decisions log
- 2026-09-23 A4 review stopping rule (Lake): after Codex round 6, any further A4 findings that are NOT blockers go to a documented residuals list instead of another fix round; A4 then moves to Lake's live check. Blockers are still fixed.
- 2026-09-23 R1 (registry lag after a mid-turn rotation; second window can claim the new id in active_sessions.json while the durable turn lease still serializes its turns): Lake chose option (c), accept and document. Both reviewers endorsed (c) or (a) and rejected (b) (synchronous handoff: deadlock risk, no data-safety gain). Revisit (a) (async re-anchor frame) only if it shows up in practice.

## Order
0 → A1, A2 → B (48bb63b8d6; ca86781a91 residual only) → D → A3 → A4 (+ window fixes) → C (analysis
started early) → phase 2b auth redesign → Group 2. Each bucket: triad before the next.

## Environment notes
- v2026.9.21 requires Node ^22.22 / ^24.11 / >=26 and npm <11.10 or >=11.17; the machine has Node 22.18 / npm 11.5.2 (unchanged).
- DECIDED (Lake): project-local pinned toolchain, no machine-wide change. Pinned Node v26.10.0 / npm 11.19.1 (win-x64), matching upstream's .nvmrc (26) and every JS CI workflow (node-version: 26). Location: `.tools/node-v26.10.0-win-x64/` in the sync repo (git-excluded via .git/info/exclude), sha256 verified against the release SHASUMS256.txt. Use it by prepending that dir to PATH; validate with engine-strict on.
