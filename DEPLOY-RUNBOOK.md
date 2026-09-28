# Hermes deploy runbook (F1/F3), draft 2026-09-28

Nothing here runs until every other line of `DEPLOY-GATE-CHECKLIST.md` is closed. Lake runs or approves every step
that touches the live install (`C:\Users\Lake\AppData\Local\hermes\hermes-agent`, the InstallDir).
Rules that apply to every step: target exact PIDs only, re-verified immediately before any kill (never kill by name or
path); never spend a live OAuth refresh token as a test; say which path (root vs `profiles\claudetriad\...`) any
number came from.

## 0. Freeze (F1)
1. Candidate = the tip of `magi/sync-v2026.9.21` after the last integration. Record its SHA here: `__________`.
2. A2 final validation runs against exactly that SHA (isolated runner, full A/B vs baseline, a written disposition for
   every remaining failure in `A2-DISPOSITIONS.md`, process-leak check). Any change after A2 = new SHA = rerun A2.
3. Tag it: `git tag deploy-candidate-<date> <sha>` (local tag only; nothing is pushed).
4. Confirm the desktop contract: if the candidate changes `apps/desktop` or `apps/shared`, a desktop rebuild is needed
   (step 3c); otherwise backend-only (`-Stage dependencies`) is enough. Check with
   `git diff --stat <live 1b8cc8c1b5>..<sha> -- apps/desktop apps/shared`.

## 1. Before closing anything
1. F7: run `scripts/predeploy_backup.py` against the live install (root and every profile) and keep its verified
   manifest. Do not continue unless it reports a complete, restore-verified backup.
2. F10: T6 runs the skills drift merge per the audit repo's `F10-PROCEDURE.md` (day before).
3. F8: have the rollback runbook (`ROLLBACK-RUNBOOK.md`) open; its prepare step runs with the NEW code, so a rollback
   is only safe after the deploy has started using the new registry. Never "roll back" by restoring an old auth.json.
4. Note the current PIDs and command lines of: desktop app, desktop backend(s), relay (`hermes dashboard`, task
   `HermesRelayDashboard`), messaging gateway (`gateway run`), compute hosts. Old messaging gateway PID 49956 is
   historical; re-identify.

## 2. Close everything
1. Quit the desktop app normally (close-all), then wait for its backends to exit.
2. Stop the relay: `schtasks /End /TN HermesRelayDashboard`, then verify its exact PID is gone; if it persists, kill that
   exact PID after re-verifying its command line.
3. Stop the messaging gateway the same way (exact PID, verified command line).
4. Confirm no process has files open under InstallDir (no `hermes`/`python` whose command line points into it). This
   replaces the built-in updater's `.hermes-update-in-progress` marker.

## 3. Install the candidate
Use `scripts\install.ps1` stages directly (never wrap in a function that re-splats `$args`):
`install.ps1 -Stage <stage> -HermesHome C:\Users\Lake\AppData\Local\hermes -InstallDir C:\Users\Lake\AppData\Local\hermes\hermes-agent -NonInteractive`
1. `git -C <InstallDir> checkout --detach <sha>` (or `-Commit <sha>`; the installer updates in place).
2. `-Stage dependencies` (venv sync). Then the dependency repair the built-in updater would do:
   - STT: `python -c "import faster_whisper, ctranslate2"` in the InstallDir venv. If `uv sync` pruned them, reinstall
     the lazy extras (ctranslate2 pinned 4.7.2) and re-check CUDA per `hermes-stt-setup`.
   - Import probe for the rest: `hermes doctor` (step 5) reports missing modules; fix before relaunch.
3. Desktop, only if step 0.4 found desktop/shared changes: copy `apps\desktop\release\win-unpacked` aside first
   (`win-unpacked.bak-<date>`), then `-Stage node-deps` and `-Stage desktop -IncludeDesktop` (cold npm ci ~2 h; the
   pinned Node is `.tools\node-v26.10.0-win-x64`, matching `.nvmrc`).
4. `web_dist`: confirm the dashboard SPA build exists and matches the candidate (the relay serves it).

## 4. First start
1. Start the desktop app. Check that no "backend out of date" toast appears (contract handshake).
2. The first runtime start performs the Codex authority bootstrap (stage 1). Expected: `hermes auth recovery list`
   shows the registered chains; nothing fenced unless evidence required it. If anything is fenced, stop and read the
   reason before signing in again.
3. Restart the relay: `schtasks /Run /TN HermesRelayDashboard`, then verify a NEW PID with a fresh StartTime (F9).
   Verify the phone path end to end.
4. Restart the messaging gateway in its window (F2) and verify its new PID.

## 5. `hermes doctor` last (F3)
Run `hermes doctor` (root, then each profile) as the final step: it performs the persisted config migration and the
import-probe repair. Afterwards review the delegation keys the v36/v37 migrations raise (3->10, 50->250) and pin them
conservatively; note the before/after `_config_version`.

## 6. Acceptance (F4, F5) and soak
1. F4: the A4 live checks (saved windows persist until Forget, new-window selection, rounds 3-6 criteria) and
   per-renderer memory telemetry surviving a reboot.
2. F5: the ~27-window workload (concurrent prompts, tools, approvals, stop, reconnect, compaction, rewind), with the
   carry-forwards: model-picker latency near its 3 s limit; desktop auto-restart after a freeze kill; keep the
   achievements plugin disabled. Verify B5 (resume auth contention), G2, D6, D8, D9 live.
3. If F5 fails: follow `ROLLBACK-RUNBOOK.md` (prepare with the new code, then switch back).

## 7. After
F6 (skill_view/day and views/write vs 200.7 / 2.21) and E3 (`hermes curator run --dry-run --consolidate`, then Lake
decides) are post-deploy.
