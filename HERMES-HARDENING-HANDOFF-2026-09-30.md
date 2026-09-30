# Hermes hardening pass: handoff to the Hermes managing session (2026-09-30)

This supersedes `HERMES-HARDENING-HANDOFF-2026-09-29.md`, which is kept as history.

You are taking over coordination of the Hermes hardening pass, from now until the full code deploy. Lake (the user) arbitrates every scope and risk decision. Your job is to coordinate:
- route work to implementers;
- relay reviews;
- keep the records current;
- integrate the branches.

Read this whole file before acting, then the files in section 4.

## 1. What's live right now (don't break it)
- **The Qwen relay is live on `claudetriad`.** It was promoted 2026-09-30 around 10:20 as a config-only change on the current live build `1b8cc8c1b5`.
  - **Relay model:** `qwen3.5:9b`, via Ollama at `localhost:11434/v1`, 65536-token context, `agent.reasoning_effort: none`.
  - **Delegated workers:** `anthropic` / `claude-opus-5`, effort `xhigh`, on the Max subscription. There's no `ANTHROPIC_API_KEY` in `claudetriad\.env`; keep it that way.
  - **Spawn depth:** `delegation.max_spawn_depth: 2`, so Opus can still launch its own sub-agents.
  - **Toolsets:** the full `hermes-cli`. Never trim them: sub-agents can't gain tools their parent lacks.
  - **Fallback:** `openai-codex` / `gpt-5.5`.
  - **Backup:** `profiles\claudetriad\config.yaml.bak-localrelay-20260930-102006`.
  - **Rollback:** `python %LOCALAPPDATA%\hermes\scripts\apply-local-relay.py --rollback --profile claudetriad`, then set `max_spawn_depth` back to 1.
- **The Qwen tool-call check** (2026-09-30, `review-artifacts\mcp-hbhc\q3-ab\`) informs the relay prompt:
  - **Current wording:** 1 direct MCP call in 80 requests.
  - **A standing "use tool_call" rule in the system prompt made it worse:** 5 of 10 turn-initial calls went direct, against 0 of 10 without it. **Don't add that rule to the relay hint.**
- **Known live-build issues until the full deploy:**
  - A direct call to a deferred MCP tool gets a false "does not exist" (H-C). Qwen recovers, at the cost of a turn.
  - A slip like `read_terminal` can be misrouted to `terminal`.
  - An unattended MCP OAuth can open a browser.
  - Config edits need a Hermes restart to take effect (A43).
  - For Unity: the `unity-*` servers are `lazy: true` and cache 0 tools while Unity is closed. `/reload-mcp` bringing their tools back only arrives with the full deploy.
- **Skills F10 is live** (2026-09-29). The Antigravity-role skills change (`bb55202` + `fe6aac9` in `hermes-skills-audit-20260922`) still awaits Codex review, then an apply window run from outside Hermes.

## 2. Rules that override everything else
- **Stopping rule (Lake, 2026-09-30):** once a track has no open P1 (or P2) bugs, stop the fix/review loop and move to deploy prep. P3 and below are recorded as deferred known limits, not fixed in more rounds.
  - Codex closure prompts ask for P1/P2 blockers only, with lower findings listed separately.
  - At the 2026-09-30 cutoff, the MCP and config/auth P2s were deferred too. Ask Lake to confirm that's acceptable before the full deploy.
  - Surface the time cost of long loops, and offer structural or scope levers instead of patching case by case.
- **Nothing ships with a known P1.** Every P1 is fixed and reviewed, disproved with evidence, or explicitly deferred by Lake.
- **Frozen scope.** A25, T3 and A28 each have a frozen `RELEASE-SCOPE-*.md`. New discoveries are classified by that file's Part 3 outcomes, and scope changes need Lake.
- **Who can close a review.**
  - Codex is the authoritative reviewer; a track closes only on Codex's CLOSE plus your confirmation.
  - **Antigravity is a defect-finder only, with no approval authority.** Ignore any "approved" it writes, and never relay it as a pass.
  - **Send Antigravity only self-contained review packets** (preface + prompt + report + full diff), never status updates. Given status updates it only agrees; given a packet it found real bugs.
  - Its standing preface is in `review-artifacts\STATUS-FOR-LAKE.md` → "Review prompts".
- **Budget.** Opus is fine; Codex/ChatGPT and Fable usage is scarce. Use bounded, P1-only Codex rechecks after a fix round. Lake pastes prompts by hand, so write plain-English, paste-ready blocks, and put long prompts in files.
- **Safety.** Every one of these has been violated before:
  - Never kill a process by name. Use exact PIDs only, and re-verify each command line and start time immediately before killing.
  - Never start, stop or terminate WSL, Docker, Ollama or any service or VM.
  - Never restart the backend or touch the live install without Lake's OK.
  - Never test live OAuth tokens, and never read or touch `~/.codex`.
  - Never use `git stash`: the stash stack is shared.
  - No network for agents, and don't load web pages.
  - Run tests only via `scripts/run_tests_isolated.py -- -j 2 <explicit file list>`. Never pass an empty list, never run bare pytest, and never run `test_exec_schtasks_round_trips_non_ascii_task_argument_live` or `test_gateway_windows.py`.
  - Evidence goes under `E:\GitHub\Magi-AGI\review-artifacts\...`, never into tracked trees.
- **You can't run the deploy windows from inside Hermes.** The skills apply and the code deploy both stop every Hermes process. Prepare them, then hand them to Lake to run from plain PowerShell or Claude Code.
- **Memory.** Before long test runs, check `commit_pct` (not free RAM). Firefox has grown past 25 GB twice; if a run is cut off, ask Lake to close Firefox normally (never force-kill it).
- **Profile scope.** State which path a number came from: the root or `profiles\claudetriad`.

## 3. Track state
| Track | Branch / worktree @ tip | State | Next |
|---|---|---|---|
| **MCP H-A..H-D + config/auth A43–A45** (integration candidate) | `magi/config-auth` @ `60448cd8e7` = mcp-hbhc `b3cbb93cd9` + config-auth, rebased cleanly | **CLOSED for P1 by Codex** (2026-09-30). 74/74 closure and contract tests pass on the combined tip. P2s deferred: `review-artifacts\mcp-hbhc\R11-DISPOSITIONS.md` and `review-artifacts\config-auth\REPORT-ROUND5.md` | Run the full related suites on `60448cd8e7` (MCP 155 files, list in `review-artifacts\mcp-hbhc\r9-files.txt` + round-10 files; config/auth 592 files, `review-artifacts\config-auth\suite-files.txt`), then merge into `magi/sync-v2026.9.21`. Get Lake's OK on the deferred P2s and the rename-window limit |
| **T3** (desktop runtime identity) | `magi/track-desktop` @ `hermes-worktrees\desktop`, `824e3bc871` (WIP) | Paused. 23 of Codex r30's 37 findings fixed; 14 open (RC5, RC6, RC7). Lake question J3 is open | Resume from `.t3-fix-r31-20260929\HANDOFF.md` under the new stopping rule: fix only the P1s among the 14, then a bounded Codex recheck. If it drags, offer Lake the backend-only deploy (the desktop keeps today's build) |
| **A25** (turn/cancel ownership) | `magi/track-a25-a19b` @ `.claude\worktrees\agent-a7ebd480a722c0b0a`, `54c9dc3577` (WIP) | Paused mid round 11. F14's test is red on purpose. F05, F08/F09 and F15–F18 open. Lake decision 5 open | Resume from `review-artifacts\a25\HANDOFF.md`, applying the P1-only rule to what's left |
| **A24** (read-failure write-backs) | `hardening/a24-deploy-gate` @ `.claude\worktrees\agent-afea0b66f62b13b8d`, `cf8164c2e5` (WIP, untested) | Paused. F06 (uncompiled Rust) not started. Lake decision 4 open | `git reset --soft HEAD~1` (never stash); verify each fix red/green; then F06 (plan: revert `411dbdd219`). See `review-artifacts\a24\HANDOFF.md` |
| **A28-Core C3** (thread starts) | `magi/a28-core` @ `hermes-worktrees\a28-core`, `676bcdf227` | Paused. F1/F2/F7 done; F3 WIP; F4–F6 and F8–F11 scripted in `review-artifacts\a28-core\c3r-wip\`. F8 includes a real bug at `tools/delegate_tool_child_run.py:432`. Lake question F9 (Python 3.13) is open | Resume from `review-artifacts\a28-core\HANDOFF.md` |
| **A28-Core C1** (subprocess sites) | `magi/a28-core-c1a` @ `71591b9860`, `magi/a28-core-c1b` @ `2140a26975` | Built, not reviewed | Cherry-pick onto `magi/a28-core` after C3, then run combined tests |
| **A28-Core rest** | — | Not started | A42, then H1–H8 and H10–H12, then I2 |
| **A28-Framework** | `magi/a28b-0b-harness` | Parked until after the deploy | — |
| **Skills: Antigravity role** | `hermes-skills-audit-20260922` `bb55202` + `fe6aac9` | Awaiting Codex | Apply window from outside Hermes: `apply_to_live.py --base eec6067 --target fe6aac9` |

**Integration technique:**
1. Create an integration worktree from `magi/sync-v2026.9.21`.
2. Merge or cherry-pick the approved range.
3. Verify the touched files.
4. Run the combined tests against the baseline worktree `.claude\worktrees\baseline-v2026.9.21`.
5. Fast-forward the sync branch.
6. Remove the worktree.

The tracks overlap: A25/A24 both touch `hermes_state*`, and A28/A25 both touch `tui_gateway`.

## 4. Files that hold the truth (in `E:\GitHub\Magi-AGI\hermes-agent-sync` unless noted)
- `DEPLOY-GATE-CHECKLIST.md`: the master gate. Update and commit it at every event. A43–A45 are CLOSED for P1.
- `RELEASE-SCOPE-A28.md`, `RELEASE-SCOPE-A25.md`, `RELEASE-SCOPE-T3.md`: frozen work lists.
- `DEPLOY-RUNBOOK.md` and `ROLLBACK-RUNBOOK.md`.
- `E:\GitHub\Magi-AGI\review-artifacts\STATUS-FOR-LAKE.md`: pending Lake decisions 1–9, and the review prompts and Antigravity preface.
- Per-track reports:
  - MCP: `review-artifacts\mcp-hbhc\` (`REPORT.md`, `R*-DISPOSITIONS.md`, `codex-*`)
  - config/auth: `review-artifacts\config-auth\` (`REPORT*.md`, `codex-*`)
  - A25, A24, A28-Core: `review-artifacts\a25\`, `\a24\`, `\a28-core\` (each has a `HANDOFF.md`)
  - T3: `hermes-worktrees\desktop\.t3-fix-r31-20260929\HANDOFF.md`

## 5. Lake decisions pending
1. **Items 1–9** in `STATUS-FOR-LAKE.md`: A28-Core lock-hold, test-double checks, the asyncio loop-close limit, A24's Rust, A25's hunt items, and the C1 budgets and behaviour changes.
2. **T3 J3:** accept that a total storage failure can't record a gateway switch.
3. **A28 C3 F9:** narrow `requires-python` below 3.13, or record 3.13 as a limit.
4. **config/auth:**
   - accept the rename-syscall write window as a limit;
   - accept that explicit `--provider` sessions lose helper routes during a failed config read.
5. **Confirm the deferred P2 lists** for MCP and config/auth.

Settled on 2026-09-30:
- keep the last-known-good provider after a failed read;
- vitest/tsc allowed with read-only junctions to desktop's `node_modules`;
- remove fuzzy tool-name repair;
- restructure the MCP borrowed-profile state (Option A);
- an explicit `/reload-mcp` bypasses the lazy cache;
- the VolcEngine XML trim stays removed.

## 6. Path to the full deploy
1. Integrate the MCP + config/auth candidate: full suites, then merge.
2. Resume T3, A25, A24 and A28-Core under the P1-only rule, with bounded Codex rechecks.
3. Integrate everything into `magi/sync-v2026.9.21`.
4. Run the I2 gates:
   - the POSIX test arm (it needs a Linux runner; Lake provides one or defers it);
   - C5 test 10;
   - the electron rerun;
   - the combined failure inventory.
5. A2 final validation on the frozen candidate SHA.
6. The deploy, per `DEPLOY-RUNBOOK.md`, run by Lake from outside Hermes:
   - F7 backup;
   - skills;
   - close Hermes by exact PID;
   - install;
   - first start;
   - relay restart (`schtasks /Run /TN HermesRelayDashboard`);
   - restart the `claudetriad` gateway;
   - `hermes doctor` last.

   Then run F4/F5 acceptance on Lake's roughly 27-window workload.
7. **Before and after the deploy, check the relay config survived** (`profiles\claudetriad\config.yaml`: model and `max_spawn_depth: 2`).

## 7. How Lake wants you to work
- **Keep Lake informed but brief:** one line per event, plus paste-ready prompts in files.
- **Say what you actually verified.** Treat agent reports as claims; they have overstated completion.
- **Ask a real question for new scope,** with a recommendation. For choices, offer options with the recommended one first.
- **Surface the time cost of fix loops early,** and offer scope levers.
