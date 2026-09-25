# Parallel hardening tracks — session prompts (2026-09-24)

Paste the COMMON PREAMBLE followed by ONE track section into a fresh Claude Code session started in
`E:\GitHub\Magi-AGI\hermes-agent-sync`. The coordinating session (the original hardening session) keeps the
credential work (checklist section B), integrates approved branches onto `magi/sync-v2026.9.21`, and owns
`DEPLOY-GATE-CHECKLIST.md`.

---------------------------------------------------------------------------------------------------------

## COMMON PREAMBLE (paste first, every track)

You are one of several parallel Claude sessions in a Hermes hardening pass. Hermes = the NousResearch agent
(fork at `E:\GitHub\Magi-AGI\hermes-agent-sync`), which Lake runs as his desktop app with 20-27 concurrent
session windows. That scale is the target, not an edge case.

**Deploy rule (Lake, non-negotiable):** nothing deploys until EVERY item in
`E:\GitHub\Magi-AGI\hermes-agent-sync\DEPLOY-GATE-CHECKLIST.md` is closed. Never propose early, partial or
"fix it later" deploys. Put effort into finding remaining issues, not into shortening the path to deploy.

**Read first:** `DEPLOY-GATE-CHECKLIST.md` (your items are named below; the closure rule at the top applies);
root `AGENTS.md` plus the area `AGENTS.md` for every directory you edit; `SYNC-MAP-v2026.9.21.md` (decisions
log, environment notes); the memory entries named in your track (under
`C:\Users\Lake\.claude\projects\E--GitHub-Magi-AGI\memory\`; they describe the incidents but may be stale, so
verify against the code).

**Workspace:**
- Create your own worktree OUTSIDE the repo, so the nested-worktree source scans don't trip:
  `git -C E:\GitHub\Magi-AGI\hermes-agent-sync worktree add E:\GitHub\Magi-AGI\hermes-worktrees\<track> -b magi/track-<track> magi/sync-v2026.9.21`
- Work and commit ONLY on that branch. Never commit to `magi/sync-v2026.9.21`. Never edit
  `DEPLOY-GATE-CHECKLIST.md`; report status to Lake and the coordinator updates it.
- Commit trailer: `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`. Keep
  `git diff --check magi/sync-v2026.9.21..HEAD` clean.

**Method:**
- **Check upstream first:** the `upstream` remote (NousResearch) is already fetched; do NOT run `git fetch`.
  Before writing any fix, `git log upstream/main` / `-S` the symbols since tag `v2026.9.21`. Our policy is
  the stable release; pulling anything from upstream main needs Lake's explicit OK.
- **Invariant tests proven RED on the base:** negative check by `git checkout <base> -- <file>` or a WIP
  commit. NEVER use `git stash` (the stash stack is shared by every session). No change-detectors, no
  source-reading tests.
- **Tests ONLY via the isolated runner, with the repo venv:**
  `E:\GitHub\Magi-AGI\hermes-agent-sync\.venv\Scripts\python.exe scripts\run_tests_isolated.py --timeout 600 -- -j 4 <paths>`
  Hard limit **-j 4** per session (several sessions share 63 GB RAM; a full parallel run has already
  exhausted memory once). Run targeted files while iterating; wider runs in batches.
- **Compare failures with the baseline worktree:** `E:\GitHub\Magi-AGI\hermes-agent-sync\.claude\worktrees\baseline-v2026.9.21`
  (v2026.9.21 + runner fix; run ITS runner on the same files). Report branch-only failures with a
  disposition each; baseline parity is "no regression", not "closed".
- **Test junk:** after runs, delete any `isolated-run-*` / mangled-name junk files the upstream
  `test_shell_hooks` drops into your tree.
- **Pinned Node for desktop/TUI work:** `E:\GitHub\Magi-AGI\hermes-agent-sync\.tools\node-v26.10.0-win-x64`
  (npm 11.19.1; matches upstream `.nvmrc`). A cold `npm ci` can take ~2 h, so reuse the npm cache.

**Hard safety rules:**
- Do NOT touch the live install (`C:\Users\Lake\AppData\Local\hermes\hermes-agent`), its config or state,
  or any running Hermes process, without asking Lake first. That includes reading live state DBs in ways
  that take locks.
- Never kill by process name or by path pattern; only exact PIDs, re-verified immediately before. Never
  restart the backend or relay without asking.
- Never test or spend live OAuth refresh tokens. Fake credentials only; read `expires_at` if you need
  status. Never read `~/.codex`.
- Don't load web pages (Lake's Firefox is closed to save memory). No network except local loopback test
  servers.
- Say which path any live number came from (per-profile `profiles\claudetriad\…` vs the Hermes root).

**Review loop (triad):**
- You are the writer. Codex (authoritative) and Antigravity review. Lake pastes your plain-English review
  prompts into his own Codex and Antigravity sessions and pastes their verdicts back to you.
- After each round, give Lake two short prompts. Each must name your branch, worktree path, commit range
  and what to check; point out the risky parts and any choices you made beyond the brief.
- Fix every blocker. After several rounds with no blockers, non-blocker nits may go to a residuals list,
  but residuals are still fixed before deploy.
- When BOTH approve, tell Lake: "Track <name> ready for integration: branch magi/track-<track>, commits
  <range>, closes checklist items <ids>". Include the files you touched, so the coordinator can spot
  overlaps with other tracks.
- **Cost:** surface running time honestly. If an item turns into a many-round investigation, say so, but
  never recommend shipping it open.

---------------------------------------------------------------------------------------------------------

## TRACK T1 — gateway: shared-gateway concurrency (`gateway`)
**Checklist:** G4, G9 (bootstrap reset storm part only), D6 (measure), D7, D4 (verify on the synced lease model).
- **G4:** implement `DESIGN-SHARED-GATEWAY-CONCURRENCY-2026-09-24.md` option A (approved), folding in its
  "Review notes (2026-09-24)" section:
  - default-executor admission at `ws.py:~360`;
  - a DISTINCT overload error code (not 5036);
  - move the single-flight bootstrap reset EARLIER in the order;
  - lanes, bounded queues with read-only fast-fail, coalescing, and hoisting overrides.
- **Lock-free config reads (part of option A) = checklist G3.** They depend on upstream-MAIN commits.
  Do not pull those unless Lake has given explicit OK (ask him at the start). If he declines, design the
  equivalent locally and say so.
- **D7 (the 09-07 whole-process freeze):** closure needs a fix or a validated mitigation, not a watcher.
  Plan native-stack capture for the case where Python dumps are identical. Live capture only with Lake's OK.
- **Acceptance:** the design's acceptance notes, plus a concurrency harness modelling ~27 windows
  (concurrent prompts, tools, approvals, stop, reconnect, compaction). This harness also feeds checklist F5.
- **Memory:** hermes-shared-gateway-concurrency-gap, hermes-config-load-rpc-pool-convoy,
  hermes-bootstrap-reset-storm, hermes-compaction-concurrent-load, hermes-model-cache-prefetch-hang,
  hermes-session-ownership-lease-race.

## TRACK T2 — compute host and child-process lifecycle (`lifecycle`)
**Checklist:** G1, A6, A12, A15, D1, D2, D3.
- **G1:** compute-host crash-loop latch reset; a safe in-process fallback, and A6: a turn through the
  fallback stalls after `message.start`; stdin-EOF instrumentation.
- **D1:** ownership and port release for a detached `gateway run` that squats :8642 through quit-all
  (consider a kill-on-close job object or explicit PID tracking).
- **D2:** the terminal-tool 420 s hang across respawns: the terminal launcher/worker tree plus pipe cleanup.
- **D3:** (a) a running post-tool hook wedging a worker; (b) a descendant keeping subprocess output pipes
  open. Orphan-future cancellation fixes neither.
- **A12:** prove an orphaned turn's running TOOL actually stops (transcript fencing only stops its writes).
- **A15:** `tests/tui_gateway/test_compute_host_phase1.py` hangs on base and branch; fix it or give a
  written disposition.
- **Windows reality:** process trees, job objects, the venv launcher/worker chain. Tests that need real
  process topology get the repo's `windows_only` marker.
- **Memory:** hermes-compute-host-crash-loop-latch, hermes-compute-host-code0, hermes-desktop-backend-pool-bug,
  hermes-terminal-tool-hang, hermes-post-tool-hook-wedge-and-subprocess-pipe-leak,
  hermes-tool-executor-orphaned-futures, hermes-process-kill-footgun.

## TRACK T3 — desktop renderer and window store (`desktop`)
**Checklist:** A3, A4, G5, G6, G7, and prep for G2/D8/D9 verification.
- **A3 (R-A4-1):** a reload can let Reopen advance before the replacement document commits.
- **A4 (R-A4-2):** a null report token passes when no document is current.
- Both are residuals from sync bucket A4 (per-window store, "Close and Forget This Window"); see
  SYNC-MAP A4 entries.
- **G5:** compaction summary growth, plus renderer V8 heap retention (two renderer OOMs, `-536870904`).
- **G6:** the "Gateway down" stale-warning reattach (renderer).
- **G7:** the dropped-submit root cause. There is a 5-line submit chain; `routedRuntimeId` has been
  globally null since 09-14.
- **G2, D9 and D8** are verified live later. Prepare the check scripts and criteria now: saved windows
  persist until explicit Forget; the new-window regression; the memory bound.
- **Toolchain:** pinned Node. The desktop is Electron (`apps/desktop/`), TS style per root `AGENTS.md`.
- **Memory:** hermes-renderer-v8-heap-oom, hermes-gateway-down-sidebar-reselect-fix,
  hermes-dropped-submit-renderer-side, hermes-window-list-frozen-snapshot, hermes-session-window-list-wipe,
  hermes-new-window-session-regression, hermes-compaction-block-renders-as-user.

## TRACK T4 — MCP startup, futures timeouts, hygiene (`mcp-hygiene`)
**Checklist:** G8, G10, G11, G9 (everything except the bootstrap reset storm), A5.
- **G8, full original scope:** discovery must not block useful service after readiness is announced.
  - Cover: cold cache; expired token (fake); unavailable server; several profiles starting at once;
    bounded discovery; child cleanup.
  - Any dedupe keeps auth/config distinctions; a matching URL is not identity.
  - Includes the schema cache `ttl_ms=0` read-gate bug.
- **G10:** the remaining futures-audit timeout sites: account/billing refresh safety, status probe,
  Home Assistant, TTS, delegation interrupt join, `context_references`.
- **G11:** plugin autocomplete bounding. Confirm whether any plugin provider is installed, by reading
  config only; don't start anything.
- **G9:**
  - a rotating backup of state.db;
  - Unity MCP backoff plus log rotation;
  - cross-process session-list staleness;
  - the prompt-null rebuild signal.
- **A5:** Yuanbao recall redaction rewrites the whole transcript unfenced; make it a targeted, fenced
  redaction.
- **Memory:** hermes-mcp-startup-refactor, hermes-mcp-schema-cache-ttl-zero, hermes-tool-executor-orphaned-futures,
  hermes-cross-process-session-list-staleness, hermes-system-prompt-null-rebuild.

## TRACK T5 — test-suite hygiene (`test-hygiene`; small, finish fast)
**Checklist:** A7, A8, A9.
- **A7:** upstream `test_shell_hooks` writes mangled-name files into the working tree on Windows. Force
  every temp path under the sandbox TEMP.
  - There is also an older untracked `%SystemDrive%/ProgramData/...` tree in the main repo root
    (2026-09-06, an unexpanded env var). Find its writer, fix it, then tell Lake the tree can be deleted;
    don't delete it yourself.
- **A8:** source-scan tests (`test_model_alias_credentials`, the config-read guard) walk nested
  `.claude/worktrees`. Make the scans ignore nested worktrees and other checkouts. Do NOT delete any
  worktree: several are in active use.
- **A9:** de-flake, or fix, the fast-lane first-chunk timing, late-ack TTL
  (`test_late_ack_handlers_are_bounded_by_ttl_and_cap`), the update-handoff-marker PowerShell test, and
  `test_peer_dm_into_open_bot_chat` (streamed). Follow the repo flake policy: event-based sync,
  wall-clock bounds ≥ 2 s.

## TRACK T6 — skills (`skills`; checklist E1, E2; E3/E4 are Lake's calls)
- **E1:** finish Pass A, the directive audit: `private-wiki-backup-environments`,
  `decko-local-browser-write-flows`, and the AUDIT sweep.
  - The audit working dir is `E:\GitHub\Magi-AGI\hermes-skills-audit-20260922\`. Lake: grant this
    session a permission rule for `E:/GitHub/Magi-AGI/hermes-skills-audit-20260922/**`.
- **E2:** Pass B, size and sprawl. Slim `triad-review-workflows` (~101 KB), `persistent-agent-triad` and
  `wiki-source-triangulation`; collapse reference sprawl; delete superseded skills. Passes A and B stay
  distinct.
- **E3/E4:** after Pass B, discuss the `curator.consolidate` decision and Lake's own skills-improvement
  ideas with him. Don't decide them.
- Skills live in the live Hermes profile. Edit copies in the audit dir; apply to live only with Lake's
  explicit OK.
- **Memory:** hermes-skill-bloat-and-consent-gates.
