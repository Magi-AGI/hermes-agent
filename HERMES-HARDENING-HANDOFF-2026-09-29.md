# Hermes hardening pass: handoff to the managing session (2026-09-29)

You are taking over coordination of the Hermes hardening pass, which runs until the final code deploy. Lake (the user)
arbitrates every scope and risk decision. You coordinate: you route work to implementers, relay reviews, keep the records
current, and integrate. Read this whole file, then the files it points to, before doing anything.

## 1. Rules that override everything else
- **No deploy with known issues.** Every identified issue is fixed and reviewed, disproved with evidence, or explicitly
  deferred by Lake. Stopping rules apply only to nits, and those still get fixed. Never suggest early or partial deploys.
  The skills-only deploy on 2026-09-29 was Lake's explicit exception.
- **Frozen scope.** A25, T3 and A28 each have a frozen `RELEASE-SCOPE-*.md`. New discoveries are classified by that file's
  Part 3 outcomes, not added as rows. A demonstrated production bug is still fixed, disproved, or explicitly deferred by Lake.
  Scope changes need Lake.
- **Who can close a review.** Codex is the authoritative reviewer: a track closes only when Codex approves and the lead
  confirms. **Antigravity is a defect-finder only, with no approval authority.** Ignore any "approved" it writes, and never relay it as a pass.
  Start every Antigravity prompt with the standing preface in `review-artifacts/STATUS-FOR-LAKE.md` → "Review prompts".
- **Budget.** Lake's ChatGPT/Codex ("Astra") and Fable usage is scarce, and Opus is fine. Use Antigravity for intermediate
  rounds, and one batched Codex closure review per track. Lake pastes the prompts into Codex and Antigravity by hand, so write
  them as plain-English, paste-ready blocks.
- **Safety** (these have all been violated at least once, so enforce them):
  - Never kill a process by name. Target exact PIDs only, and re-verify the command line and start time immediately before killing.
  - Never start, stop or terminate WSL, Docker or any service or VM.
  - Never restart the backend, or touch the live install `C:\Users\Lake\AppData\Local\hermes\hermes-agent`, without Lake's OK.
  - Never test live OAuth refresh tokens, and never read or touch `~/.codex`.
  - Never use `git stash`: the stash stack is shared.
  - No network for agents. Don't load web pages.
  - Tests run only through `scripts/run_tests_isolated.py -- -j 4` (use `-j 2` when two or more agents are testing), with explicit file lists written to disk first, because an empty list runs the full suite.
  - Never run `test_exec_schtasks_round_trips_non_ascii_task_argument_live`.
  - Reviewers and agents write outside tracked trees: `E:\GitHub\Magi-AGI\review-artifacts\...`.
- **Profile scope.** Assume per-profile scope (`profiles\claudetriad\...`), and say which path any number came from.
- **Estimates** are in agent time, not human hours.

## 2. You cannot run the deploy window from inside Hermes
Both remaining live steps stop every Hermes process: the skills apply and the final code deploy. A Hermes session would be
killing itself. When those steps come, prepare everything, then hand them to Lake to run from outside Hermes (plain
PowerShell or a Claude Code session). The steps are in `DEPLOY-RUNBOOK.md` and the skills scripts below.

## 3. Source-of-truth files (all in `E:\GitHub\Magi-AGI\hermes-agent-sync` unless noted)
- `DEPLOY-GATE-CHECKLIST.md`: the master gate. Every item and its status. Update and commit it at every event.
- `RELEASE-SCOPE-A28.md` (frozen), `RELEASE-SCOPE-A25.md` (frozen), `RELEASE-SCOPE-T3.md` (frozen): the work lists.
- `DEPLOY-RUNBOOK.md` (F1/F3) and `ROLLBACK-RUNBOOK.md` (F8).
- `E:\GitHub\Magi-AGI\review-artifacts\STATUS-FOR-LAKE.md`: the current status, **decisions pending for Lake (items 1–9)**,
  and paste-ready review prompts.
- Per-track implementer handoffs:
  - `review-artifacts\a25\HANDOFF.md`
  - `review-artifacts\a24\HANDOFF.md`
  - `review-artifacts\a28-core\HANDOFF.md`
  - `E:\GitHub\Magi-AGI\hermes-worktrees\desktop\.t3-fix-r31-20260929\HANDOFF.md` (T3; round 30 is `.t3-fix-r30-20260929\`)

## 4. Track state at handoff (each implementer was told to pause at a clean commit and write its handoff)
| Track | Branch / worktree | State | Next |
|---|---|---|---|
| **T3** (desktop runtime identity) | `magi/track-desktop` @ `hermes-worktrees\desktop` | Round 31 in progress: Codex r30 closure found 37 findings (12 P1), plus 5 Antigravity P1s | Finish r31: adopt the probes, widen each row's test to its full property, fix by root cause. Then a Codex closure review. If the count doesn't drop sharply, bring Lake the **backend-only deploy** option (the desktop keeps today's build; T3 continues post-deploy). Codex agrees to decide after r31 |
| **A25** (turn/cancellation ownership) | `magi/track-a25-a19b` @ `hermes-agent-sync\.claude\worktrees\agent-a7ebd480a722c0b0a` | Round 11 in progress: Codex r10 closure found 18 findings plus 6 failing probes; Antigravity found mutation-control path fragility | Finish r11, then a Codex closure review. Lake decision 5 (deferred build vs reset; transcript-edit ownership) is still open |
| **A24** (read-failure write-backs) | `hardening/a24-deploy-gate` @ `hermes-agent-sync\.claude\worktrees\agent-afea0b66f62b13b8d` | **Paused** at `cf8164c2e5` (base `75f8592900`). `6748c91c8b` adopts the probes (9 fail at base). `cf8164c2e5` is a labelled WIP commit with untested fixes for F08/P2/F03/F07/F01-F02. F06 (Rust) is not started; the plan is to revert `411dbdd219`, among other steps, per its HANDOFF | Resume: `git reset --soft HEAD~1` (never stash), verify each fix red/green, commit one at a time, then F06. **Codex's gate:** all 7 of its r10 probes pass, AND the Rust updater is validated or out of the correctness path. Lake decision 4 (the uncompiled Rust F06) is open |
| **A28-Core C3** (thread starts) | `magi/a28-core` @ `hermes-worktrees\a28-core` | C3 fix round in progress: Codex found 11, plus Antigravity's harness P1 (`thread_faults` name matching) | Finish, then a Codex C3 closure review |
| **A28-Core C1** (subprocess sites) | `magi/a28-core-c1a` (S1–S9 + A41, tip `71591b9860`); `magi/a28-core-c1b` (S10–S17, tip `2140a26975`) | Built, not reviewed | Cherry-pick both onto `magi/a28-core` after the C3 fixes, then run the combined tests. Worktree `a28-core-c1b-s15` (and its `-parent`) can be removed once clean |
| **A28-Core rest** | — | Not started | A42 (two asyncio subprocess leaks), then compute-host rows H1–H8 and H10–H12 (H9/I1 belong to A25), then I2, the combined release candidate |
| **A28-Framework** | `magi/a28b-0b-harness`, design r18 | Parked post-deploy | Nothing before deploy |
| **Skills: Antigravity role change** | `E:\GitHub\Magi-AGI\hermes-skills-audit-20260922` commits `bb55202` + `fe6aac9` | Awaiting the Codex review (prompt in STATUS-FOR-LAKE) | After Codex approves: apply with Hermes stopped, from outside Hermes: `apply_to_live.py --base eec6067 --target fe6aac9` (dry run, then `--apply`, then `--verify`). Adapt `F10-DEPLOY-WINDOW.ps1` (backup first, stop on the first failure) |
| **Skills: F10** | — | **Deployed live 2026-09-29** | Backups: `E:\hermes-skills-backups\claudetriad-skills-20260929T015402` |

**Integration technique** (used throughout):
1. Create an integration worktree from `magi/sync-v2026.9.21`.
2. Cherry-pick each approved branch range.
3. Verify every touched file against the approved branch.
4. Run the combined tests against the baseline worktree `.claude\worktrees\baseline-v2026.9.21`.
5. `git merge --ff-only` into the sync branch.
6. Remove the worktree.

## 5. Lake decisions pending
These are items 1–9 in `review-artifacts/STATUS-FOR-LAKE.md` → "Decisions needing Lake", each with a recommendation:
- A28-Core lock-hold, test-double checks, and the asyncio loop-close limit;
- A24's uncompiled Rust;
- A25's two hunt items;
- the C1 budgets and behaviour changes.

Get Lake's rulings, record them in the scope files and the checklist, and route them.

## 6. What remains before the code deploy (in order)
1. Close T3, A25, A24 and A28-Core C3 (fix rounds, then Codex closure).
2. A28-Core: merge C1, fix A42, build H1–H12, review; then I2, the combined candidate.
3. Integrate every track into `magi/sync-v2026.9.21`. The tracks overlap: A25 and A24 both touch `hermes_state*`; A28 and A25 both touch `tui_gateway`.
4. The I2 gates:
   - the POSIX test arm (it needs a Linux runner with test dependencies, and Lake must provide one or defer it);
   - C5 test 10 (after A24's strict loader lands);
   - the electron rerun;
   - the combined failure inventory.
5. A2 final validation against the frozen candidate SHA.
6. The deploy per `DEPLOY-RUNBOOK.md`: F7 backup, F10 skills, close by exact PID, install, first start, relay restart, `hermes doctor` last. Then F4/F5 acceptance on Lake's ~27-window workload.

A realistic estimate at handoff is about 2 weeks. The pacing factor is the review cycles, not coding.

## 7. Working style Lake expects
- **Keep Lake informed but brief:** one line per event, plus a batched status file with paste-ready prompts.
- **Before recommending anything,** say what you actually verified. Don't relay agent claims as fact, and read reports critically: agents have overstated completion.
- **A new or expanded scope needs a real question** to Lake, with a recommendation.
- **Surface the time cost** of long fix loops, and offer scope levers (for example, the backend-only deploy for T3) rather than looping silently.
