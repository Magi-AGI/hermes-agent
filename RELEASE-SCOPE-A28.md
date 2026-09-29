# A28 release scope: A28-Core (before deploy) and the disposition of every A28b finding

Draft for Lake's approval to freeze scope. Written 2026-09-28.

## Baseline and rules

- **Code baseline.** `magi/sync-v2026.9.21` at `8abf2a0e36`. Its only difference from `42a50ebb33` is `DEPLOY-GATE-CHECKLIST.md`, so every file:line below holds at both commits. The line numbers were re-read at the tip; they are not copied from the design.
- **Split.** Lake split A28b on 2026-09-28:
  - **A28-Framework** is post-deploy and parked. It covers `test-evidence/t5-test-hygiene-2026-09-24/a28b/DESIGN.md` r18 (SHA-256 `f5d6afbd…e795`), every `DESIGN-r*.md`, and the 0b harness on `magi/a28b-0b-harness` (`6bc0aee1eb`).
  - **A28-Core** ships before deploy.
- **What A28-Core is.** Fixes to existing code, delivered as complete slices. Each slice is verified by direct behavioural tests: event-ordered tests, fake effects, and contained real-process tests. The design's state tables are documentation only and are not an acceptance oracle.
- **Two properties hold even though the framework is deferred:**
  - **P-OWN (C9).** Outstanding cleanup always has an owner and a bound on how much can accumulate. A small explicit mechanism is allowed.
  - **P-FAIL (C5, C6).** A security check that cannot run keeps its configured fail policy.
- **How "absent from the release candidate" is confirmed (used by Part 2 "framework-only"):**
  - `git ls-tree -r --name-only magi/sync-v2026.9.21` has 0 paths matching `tests/sm_harness`, `hermes_cli/_sm.py`, `subprocess_bounded` or `scripts/ci/check_subprocess_sites`.
  - `git grep -F` over `*.py`, `*.toml` and `*.yml` at the tip has 0 files containing any of: `subprocess-capacity`, `SubprocessCapacityError`, `ReaderStartError`, `live_entry`, `hermes_cli._sm`, `sm_harness`, `HostShuttingDown`, `send_indeterminate`, `terminal_shutdown`.
  - I2 re-runs both checks on the combined release candidate.
- **Every row's status is OPEN.** Nothing has been implemented.

## Part 1: A28-Core rows

### C1: the original exposure (subprocess sites with pipes)

- **The exposure (Windows).** Three call styles start `threading.Thread` pipe readers through stdlib `_communicate`:
  - `subprocess.run` / `check_output` with pipes and a timeout;
  - `Popen.communicate`;
  - Popen with the site's own `Thread.start`.
  
  `Thread.start()` waits for the new thread with no bound. After a timeout, cleanup calls `communicate()` again with no bound. Under commit pressure, either wait can hang the caller or raise `RuntimeError`, and that error escapes handlers written for `OSError`.
- **Property (every live-session site).** All four must hold:
  1. **Containment before execution.** On Windows the child is in a job before it runs: `spawn_command` or `spawn_server`, `hermes_cli/local_runtime/processes.py:115-199`.
  2. **Safe capture startup.** Pipe readers and stdin writers start through a start primitive that cannot wait unbounded, the A23 `_PipeReaders` raw-thread pattern at `hermes_cli/_subprocess_compat.py:563-641`. If a start is refused, the site sees an `OSError` subclass and gives its existing error outcome.
  3. **Bounded, app-controlled wait and cleanup.** The call returns within the caller's timeout plus a fixed cleanup constant (proposed 2 s). The whole child tree is dead or handed to C9.
  4. **POSIX unchanged.** stdlib uses selectors there, so no threads are started. Lake's earlier ruling that POSIX process groups stay unchanged holds.
- **Classification at the tip** (census re-located at `8abf2a0e36`; the source inventory is `test-evidence/t5-test-hygiene-2026-09-24/cluster/a28b/SURVEY.md` with `a28b/r2/*.md` and `classified.md`):

  | class | count | how it is handled |
  |---|---|---|
  | live-session, Windows | 147 | 143 to convert in slices S1–S16. Of those, 3 are inline-read Popen (B14, C17, D01): no thread start, but the read has no bound. |
  | already bounded | 3 | D02 `hermes_cli/banner.py:184`, T23 `tools/environments/local_gitbash_probe.py:51`, TUI-2 `tui_gateway/host_supervisor.py:86` |
  | gone | 1 | TUI-1: `tui_gateway/compute_host.py:669` now uses psutil |
  | other: live but POSIX-only | 62 | Reason: selectors, no threads. Exception: P8 `tools/file_operations_search.py:340` (+Thread :359-360) is an own-thread owner, so it is converted in S8. |
  | other: TOOLING | 71 | Reason: reachable only from operator CLI subcommands (`hermes update`, `desktop`/`gui`, `setup`, `doctor --fix`, `uninstall`, secrets/claw CLIs, `verify`, `egress`, `batch_runner.py`), never inside a serving backend. The list is in the census handback. |
  | other: NOPIPE / asyncio / SDK-owned | 75 | Reason: stdio is inherited, DEVNULL or a file (no reader threads); or asyncio subprocess (no threads); or the MCP SDK `stdio_client` owns the pipes. |
  | other: new since the inventory | 1 | `hermes_cli/kanban_db_dispatch.py:2879` is NOPIPE. The other sites new since the inventory are all bounded: `_subprocess_compat.py:664` `launch_outside_job`, and `spawn_command` at `processes.py:173/180` (the pipes belong to T22/T30). |

- **Common acceptance test (applies to every slice).** Each item is pass/fail on Windows, run in a sacrificial job:
  - **(a) Refused start.** Make the thread-start primitive raise `RuntimeError` (or `MemoryError`). The site returns its documented failure outcome within timeout + 2 s. Pass requires all of:
    - the exception is an `OSError` subclass where the site catches `OSError`;
    - the child tree is dead (psutil);
    - open handle and fd counts are back to their starting values;
    - no registry, map or session entry is left behind.
  - **(b) Stalled start.** Make the start primitive block forever. The call still returns within timeout + 2 s.
  - **(c) Pipe held by a grandchild.** The real child spawns a grandchild that inherits stdout and sleeps, then the child exits. The call returns within timeout + 2 s, and both processes are dead.
  - **(d) Large I/O.** More than 64 KB of stdin while the child writes more than 64 KB to stdout completes without deadlock. The stdin writer is its own raw thread, and a failed stdin write is reported rather than hidden: this covers CX-d15-F16 and AG-d1-G2.
  - **(e) Timeout keeps partial output.** Where a site uses `TimeoutExpired.output`, the exception carries the bytes read so far (AG-d1-G3).
  - **(f) POSIX.** The site's existing tests pass unchanged.
  - **(g) Reachability.** Each converted site's reachability and platform gate are re-traced at the tip. The census re-located the lines but took reachability from r2 at `b6827ff89f`.
- **C1.0 shared primitive.** Extend the existing A23 helpers in `hermes_cli/_subprocess_compat.py` (`bounded_probe_run` :497-560, `_PipeReaders` :563-641, `kill_process_tree` :437-494) to cover stdin input, streaming readers and own-thread starts. No new framework module is added.
  - **Acceptance:** tests (a)–(f) against a toy child.
  - **Depends on:** C2.1.
  - **Status:** OPEN.

Slices. Every slice depends on C1.0 and must pass tests (a)–(g). The slice-specific acceptance is listed with each one.

| slice | sites at the tip (id: file:line) | slice-specific acceptance | status |
|---|---|---|---|
| C1.S1 @-completion and context refs | TUI-9a `tui_gateway/methods_complete_helpers.py:30`; TUI-9b :34-35; A3 `agent/context_references.py:398` (+Thread :420/:424; callers :443 git, :603 rg); D19 `hermes_cli/commands_completion.py:382` | A3 keeps its output-bound drains (`_OUTPUT_CAP_EXCEEDED_RETURNCODE`). `_expand_git_reference` (:441-447) returns a reference error, not an exception, on a refused start (AG-d3-O1). | OPEN |
| C1.S2 shell hooks | A9 `agent/shell_hooks.py:339` (+communicate :352, :357) | A refused start is caught by the existing `_POPEN_ERRORS` handling, and the hook reports failure. | OPEN |
| C1.S3 terminal guards | T32 `tools/self_repo_guard.py:447`; T38 `tools/tirith_security.py:539`; T37 `tools/tirith_security.py:234` | Covered with C6: each failure maps to that guard's fail policy. | OPEN |
| C1.S4 checkpoint and gitlock | T11 `tools/checkpoint_manager.py:218` (wrapper, 24 callers); B23 `hermes_cli/gitlock.py:37` | A checkpoint failure is reported as a skipped checkpoint, and the turn continues. | OPEN |
| C1.S5 terminal streams, background registry and kill | T22 `tools/environments/local.py:956/958` (+`_pipe_stdin` :963; drain `base.py:378/414`, `base_output.py:477-478`); T18 `tools/environments/base_output.py:256` (stdin writer :246-248); T30 `tools/process_registry.py:1166/1168` (+`_track_started` :1078-1091, where `Thread.start` runs under `self._lock` at :1086-1091); T29 `process_registry.py:898`; GW-5 `gateway/status.py:418`; foreground-to-background handoff `process_registry.py:1202` `adopt_local` via `tools/terminal_tool_background.py:225` | 1. No `Thread.start` runs under the registry lock. 2. A refused reader start leaves no `_running` entry and kills the child. 3. Across the foreground-to-background handoff: exactly one owner reads the pipes; the registry entry becomes visible only once ownership is committed; a failed handoff withdraws it and returns the pipes to the foreground owner; incremental UTF-8 decoder state survives (a multibyte character split across the handoff decodes once); a Stop issued during the handoff is honoured by whichever side owns the process afterwards. 4. Kill fallbacks are bounded. | OPEN |
| C1.S6 long-lived clients | TUI-3 `tui_gateway/host_supervisor.py:444` (+Thread :456, cleanup :448-468); TUI-6 `tui_gateway/server.py:271` (+Thread :275-276); A5 `agent/copilot_acp_client.py:360` (+Thread :395-396); A11 `agent/transports/codex_app_server.py:146` (+Thread :160-163); T13 `tools/code_kernel.py:638` (+Thread :652-655); T15 `tools/computer_use/cua_backend_daemon.py:169` (+Thread :172) | Partial-start: a refused second thread start terminates the child and leaves it out of every registry (`_active_processes`, `_SlashWorker` owner, kernel LRU). Pairs with C4. | OPEN |
| C1.S7 MCP stderr and SSH sync | T28 `tools/mcp_tool_stderr.py:104-108`; T25 `tools/environments/ssh.py:208/211` (+communicate :219, :222); T26 `ssh.py:253` | T28 together with C7. T25 is a composite owner (a `tar \| ssh tar` pair): a failure in either half kills both and closes the shared pipe. The MCP event loop never blocks on a thread start. | OPEN |
| C1.S8 cron, relay, tts, web build, rg | CRON-1 `cron/scheduler_script.py:377` (+communicate :400 poll loop, :220 drain); CRON-2 :159; CRON-3 `cron/scheduler.py:3820`; D37 `hermes_cli/quiet_single_query.py:119` (+communicate :126, Thread :128-129); T40 `tools/tts_command_provider.py:153` (+Thread :172-175); C27 `hermes_cli/main_web_build.py:227` (+Thread :244-245); C28 :365; P8 `tools/file_operations_search.py:340` (+Thread :359-360, POSIX own-thread) | CRON-1 keeps its communicate-timeout-and-retry polling semantics (CX-d1-F1): a timeout inside the poll loop is not treated as a run failure. D37's two thread layers are both covered. | OPEN |
| C1.S9 tui_gateway rest and user shell commands | TUI-4 `host_supervisor.py:772` (only for a host with no job); TUI-5 `tui_gateway/methods_prompt.py:899`; TUI-7 `tui_gateway/methods_tools.py:185` (via `_capture_run_kwargs` :172); TUI-8 :514; ROOT-1 `cli.py:1229` | A user `!cmd` or quick command that times out returns the timeout message, with its tree dead. | OPEN |
| C1.S10 gateway platforms | GW-1 `gateway/platforms/base.py:71`; GW-2 `gateway/platforms/signal.py:114`; GW-3 `gateway/platforms/webhook.py:817`; GW-4 `gateway/platforms/webhook_filters.py:193` (input=) | A media transcode failure falls back as it does today. | OPEN |
| C1.S11 hermes_cli serve backend | A08 `hermes_cli/gateway.py:2018`; A14 `profiles.py:380`; A15 `profiles.py:1276`; C07 `local_runtime/binaries.py:254`; C08 `local_runtime/bootstrap.py:36`; C10 `local_runtime/hardware.py:151`; C11 :210; C12 `local_runtime/supervisor.py:101`; C13 `web_routers/local_models.py:512`; C14 `web_git.py:35` (wrapper); C15 `web_routers/files.py:223`; C16 `web_routers/messaging.py:379`; C17 :412 (inline read); C18 `web_server_memory.py:120`; C19 `tools_config_cua.py:55` (wrapper, 9 callers); C20 :771 (+:773, :626); C21 `git_credentials.py:207/213/229` (wrapper); C22 :73; C23 :102; D23 `doctor_state.py:420`; D24 `doctor_tools.py:29`; D25 :394; D26 `dump.py:45`; D28 `kanban_db.py:89` (wrapper, 14 callers); U14 `gateway_windows.py:196` | Every HTTP handler returns an error response (never hangs the request) on a refused start. | OPEN |
| C1.S12 startup, install, runtime repair, TUI launch | B01 `hermes_cli/_early_recovery.py:150` (no timeout today); B02 :314; B04 `main_install_repair.py:811`; B08/B09/B11 `managed_uv.py:211/217/406`; B12 :449, :457; B13 :598; B14 :642 (inline read); B15 :673; B16 :898; B18 :1109; B19 `sqlite_runtime.py:78`; B20/B21 `npm_engine.py:155/185`; B39 `update_cmd.py:191`; C01–C04 `main_tui_launch.py:346/386/481/515`; C05 `main.py:1227`; C06 `main.py:1306`; ROOT-2 `hermes_constants.py:445` | A startup probe that cannot run is reported as "unknown" or as a failed probe. Startup continues, or stops with a visible error; it never hangs. B01 gets a timeout. | OPEN |
| C1.S13 REPL and agent-turn helpers | D01 `hermes_cli/bang_shell.py:116` (inline read); D07 `cli_commands_mixin.py:1502`; D10 `clipboard.py:33`; D16 :258; D20 `copilot_auth.py:136`; D27 `goals.py:380`; C39 `windows_ssh_runtime.py:372`; C40 :440; C41 :441 | `run_gate` (D27) reports the gate as failed, not passed, when it cannot run. | OPEN |
| C1.S14 kanban and worktree git | D30 `hermes_cli/kanban_db_workspace.py:54`; D31 :307; D32 :312; D33 `kanban_pr_acceptance.py:31`; D42 `worktree_gc.py:54` (wrapper); D43 `worktree_ops.py:36` (wrapper, 37 call lines); D44 :667 | A worktree removal decision is never taken on a failed probe. | OPEN |
| C1.S15a tools: browser | T3 `tools/browser_lightpanda.py:118`; T4 `browser_tool_install.py:270`; T5 `browser_tool_lifecycle.py:498`; T6 `browser_tool_real_profile.py:60`; T7 :205; T8 `browser_use_cli.py:296`; T9 :576; T10 :592 (+:597, :601) | Tool calls return a tool error. | OPEN |
| C1.S15b tools: computer_use | T14 `tools/computer_use/cua_backend.py:135` (wrapper, 10 callers); T16 `cua_backend_session.py:105`; T17 `permissions.py:52` | Tool calls return a tool error. | OPEN |
| C1.S15c tools: container backends | T19 `tools/environments/docker.py:826`; T20 :990; T21 :1080; T24 `remote_common.py:81` (wrapper, 16 callers); T36 `tools/terminal_tool_backends.py:341` | Tool calls return a tool error. | OPEN |
| C1.S15d tools: media | T31 `tools/read_extract.py:253`; T39 `transcription_audio.py:46` (wrapper); T41 `tts_tool_delivery.py:214`; T42 `tts_tool_local.py:54`; T43 `vision_tools_image_prep.py:148` | Tool calls return a tool error. | OPEN |
| C1.S15e tools: git and misc | T1 `tools/async_delegation_recovery_hints.py:50`; T2 `bot_mode_dm.py:418` (no timeout today); T12 `code_execution_env.py:194`; T27 `lazy_deps.py:539`; T33 `skillevaluator_scan.py:107`; T34 `skills_hub_github.py:117`; T35 `subagent_worktree.py:32`; T44 `working_diff.py:36` | T2 gets a timeout. `skillevaluator` treats a scan that cannot run as not-passed. | OPEN |
| C1.S16 agent rest | A1 `agent/anthropic_adapter.py:263`; A2 `command_token_source.py:53`; A4 `copilot_acp_client.py:93`; A6 `lsp/install.py:173`; A7 `proxy_sources/iron_proxy.py:314` (wrapper); A8 `secret_sources/base.py:227` (wrapper); A10 `skill_preprocessing.py:58`; A12 `transports/codex_app_server.py:361`; A13 `vault_backends/base.py:66` (input=); A14 :82 | A secret mint that cannot run returns the backend's existing error. The secret is never logged in a failure message. | OPEN |

### C2: A33, `bounded_git_probe` / `bounded_probe_run` cleanup overrun and caller behaviour

- **C2.1 Overrun.**
  - **Property:** after a probe timeout or failure, `bounded_probe_run` returns within `timeout + 2 s` under load.
  - **Code:**
    - `_subprocess_compat.py:545-556`: `_close_job(job)` already kills the tree.
    - It then also calls `kill_process_tree(proc)`, which goes to `agent/deadline.py:401-427`, taskkill `timeout=15` at :419-423.
    - Then a 1 s drain.
    - The fix is to skip the redundant tree-kill when the job close succeeded, and to cap any fallback taskkill by the remaining cleanup budget.
  - **Acceptance:**
    - A real child that ignores termination and a grandchild holding the pipe. The probe returns in ≤ timeout + 2 s over 50 runs while 8 CPU-burner processes run, and both processes are dead afterwards.
    - With the job close forced to fail, the fallback taskkill is capped by the remaining budget.
  - **Depends on:** none.
  - **Status:** OPEN.
- **C2.2 Refused probe cached as a fact.**
  - **Property:** a probe that timed out or was refused is not cached as a negative answer.
  - **Code:**
    - `_subprocess_compat.py:299-307`: `_safe_directory_cache[cache_key]` stores `[]` permanently when both scopes fail (AG-d11-F6).
    - `tui_gateway/git_probe.py:112-121`: `_RootCache.resolve` stores a 30 s "not a repo" negative for any `""` (AG-d11-F7). That feeds project grouping, the workdir anchor and cwd reconcile.
  - **Acceptance:**
    - A fake probe returns None (refused) and then a value. The second call re-probes and returns the value.
    - A real "not a repo" answer (returncode 128) is still cached.
  - **Depends on:** C1.0.
  - **Status:** OPEN.
- **C2.3 Decision callers treat "could not run" as "no".** Each caller either distinguishes refused/timeout from a negative answer, or is recorded as cosmetic.
  - **Must distinguish:**
    - `hermes_cli/dashboard_procs.py:50-53`: `None` becomes `[]`, and the stale-dashboard kill is skipped. The docstring says it raises.
    - `hermes_cli/gateway.py:716, :731, :637-639`: gateway discovery and restart.
    - `tools/environments/local_gitbash_probe.py:106`: a rejected bash candidate is cached.
    - `hermes_cli/claw.py:112, :120`.
    - `tui_gateway/host_supervisor.py:90-92`: `_build_sha` returning `""` skips build validation at :482.
  - **Recorded as cosmetic:**
    - `hermes_cli/web_routers/git.py:69` (the gh pill);
    - `local_gitbash_probe.py:51` (ASLR text);
    - `git_probe.branch()` (a 2 s label);
    - `hermes_cli/banner.py:184` (the update notice);
    - `agent/coding_context.py:431`: shown as unknown, but pinned for the session.
  - **Already correct:** `hermes_cli/update_cmd_deps.py:108` (it fails the update).
  - **Acceptance:** for each "must distinguish" caller, a fake probe returning None produces the "unknown / retry" outcome, not the negative-fact outcome.
  - **Depends on:** C2.1.
  - **Status:** OPEN.

### C3: A34, thread pools and thread starts in live-session code

- **Property.** In a live-session path, a refused or stalled worker start (`ThreadPoolExecutor.submit` starting a worker, `asyncio.to_thread` / `run_in_executor` growing the default executor, or `threading.Thread.start`) ends as one of two outcomes within a bound:
  - the request's defined error, with no lock held and no registration, lease or child stranded; or
  - (pools) a submit to already-running workers.
  
  No `Thread.start` runs under a lock that the new thread or a completion path needs.
- **Code.** 527 grep hits in `tui_gateway gateway hermes_cli/web_server*.py hermes_cli/web_routers hermes_cli/{console_engine,desktop_console}.py cron agent tools acp_adapter`:

  | primitive | hits |
  |---|---|
  | `ThreadPoolExecutor(` | 39 |
  | `asyncio.to_thread` | 300 |
  | `run_in_executor(` | 29 |
  | `threading.Thread(` | 159 |

  A per-file heuristic puts 338 on the hot path, 92 tooling, 85 startup/background and 12 cron.
- **Hot-path sites named individually:**
  - `tui_gateway/transport.py:258` (a drain thread per write burst);
  - `methods_prompt.py:719` (the inline turn thread);
  - `server.py:1140, :2744`;
  - `session_auto_continue.py:235`;
  - `rpc_coalesce.py:42` and `rpc_lanes.py:43` (module pools);
  - `ws.py:88-89` (pools) and :102, :393 (per-request `run_in_executor`);
  - `git_probe.py:175`;
  - `acp_adapter/server.py:46, :869`;
  - `gateway/run.py:4287, :4302, :4316`;
  - `gateway/run_turn.py:1292, :3426, :3547`;
  - `agent/tool_executor.py:635, :925, :1523`;
  - `hermes_cli/web_server_chat.py:74`.
- **Exclusion list (proposed; each exclusion must cite its evidence in the slice PR):**
  - **tooling:** operator CLI or dashboard admin, not a session;
  - **startup:** runs once before serving, so a failure is a visible startup failure;
  - **`asyncio.to_thread` sites whose awaiting request fails cleanly** when the default executor cannot grow: nothing is registered before the await, and nothing is held across it;
  - comment, docstring and definition lines (9).
- **Acceptance:**
  - For each included site, patch the start/submit primitive to raise, then to block. The request returns its error within a bound. No lock is left held (a second request proceeds), and no registry entry, lease or child is left.
  - A per-site classification file lists every one of the 527 hits as included, or excluded with its reason.
- **Depends on:** C1.0 (shared raw-thread start helper).
- **Status:** OPEN. Needs Lake: the scale; see "Needs Lake" item 2.

### C4: A35, the four long-lived child trees

- **Property.** On Windows, the Copilot ACP client (A5), the Codex app-server (A11), the code kernel (T13) and the slash worker (TUI-6):
  - are spawned into an `on_owner_death` (kill-on-close) job, through `spawn_server` at `hermes_cli/local_runtime/processes.py:165-199`;
  - die with their whole tree when the backend dies, including a hard `TerminateProcess`.
  
  Their handle and transport contracts are preserved:
  - stdio JSON-RPC framing;
  - `served_profile_child_env`;
  - T13's parent-death handle passed through `STARTUPINFO.lpAttributeList` handle_list (`tools/code_kernel.py:581-603`), together with its POSIX death pipe (:630-633);
  - `start_new_session` / `CREATE_NO_WINDOW` flags.
  
  Only the operations are bounded: start, close and partial-start cleanup. The service lifetime is not. POSIX is unchanged.
- **Code:**
  - A5 `agent/copilot_acp_client.py:344-375` (Popen :360-364; pumps :395-396 start before the `try` at :429; `_release_process` :301-317 kills the direct child only);
  - A11 `agent/transports/codex_app_server.py:91-163` (Popen :146-149, threads :160-163 with no rollback; `close()` :181-203 uses a 15 s taskkill);
  - T13 `tools/code_kernel.py:612-656` (Popen :638-645, threads :652-655; watchdogs :94-177);
  - TUI-6 `tui_gateway/server.py:236-278` (Popen :271-274, drains :275-276). Its ppid watchdog, `tui_gateway/slash_worker.py:40-42, :56-64`, never fires on Windows because the ppid does not change when the parent dies.
- **Acceptance (contained real-process tests):**
  1. **Hard backend death.** Each client runs in a sacrificial parent, the child spawns a grandchild, and the parent is killed with `TerminateProcess`. Both are gone within 5 s.
  2. **T13 handle contract.** The kernel's parent-death watchdog still fires when the job is not used (POSIX path), and the handle_list inheritance still works together with `CREATE_SUSPENDED` and job assignment.
  3. **Partial start.** Refusing each thread start in turn leaves no process alive and no registry entry.
  4. **Bounded close.** `close()` returns within 2 s with the tree dead.
  5. **Transports.** The existing transport tests for all four pass.
- **Depends on:** C1.0, C1.S6.
- **Status:** OPEN.

### C5: A36/A39, Tirith breaker

- **Property.**
  - **A36 (Lake 2026-09-28).** With `tirith_fail_open=False`:
    - while the breaker is OPEN, a check is denied with a visible reason;
    - during HALF_OPEN, every caller except the one that owns the probe is denied.
    
    With `fail_open=True`, the 300 s allow is unchanged.
  - **A39.** A completion from a scan that started in an earlier breaker epoch changes no breaker state:
    - a stale crash does not count and does not re-arm `_circuit_open_at`;
    - a stale success does not close a breaker opened after that scan started.
    
    This holds across TTL expiry and concurrent checks. The stale caller still receives its own verdict, with a crash mapped through its fail policy (CX-d17-F23).
- **Code:** `tools/tirith_security.py`:
  - `_record_tirith_crash` :97-103 (re-arms on every crash while open);
  - breaker :519-529 (`allow` at :526 regardless of `fail_open`);
  - crash paths :543-557;
  - lock-free success reset :561-564;
  - config :56.
  
  Callers: `tools/approval.py:659-671` and :1138-1150.
- **Before implementing:** check upstream first (Lake's A36 note).
- **Acceptance** (event-ordered tests, with a fake clock and a fake scanner):
  1. `fail_open=False`, 3 crashes, then a check: `block` with the reason "circuit breaker".
  2. TTL expires and two concurrent checks run. The owner scans, and the other is blocked (fail-closed) or allowed (fail-open).
  3. The ported strict xfail `test_a_stale_crash_while_open_does_not_rearm_the_breaker` (from `magi/a28b-0b-harness`) passes as a plain test.
  4. A stale success arriving after the breaker reopened leaves it OPEN.
  5. A stale crash's caller receives `_fail(fail_open)`.
- **Depends on:** none (the S3 conversion lands with C6).
- **Status:** OPEN.

### C6: Security checks keep their fail policy when they cannot run (P-FAIL)

- **Property.** When a security check cannot run (spawn `OSError`, timeout, refused or stalled reader start, `RuntimeError`), the result follows that check's configured policy and never falls through to "allow" by accident.
- **The three checks:**
  - **(a) Self-repo guard.** `tools/self_repo_guard.py:445-451` `_read_git_alias` does `suppress(OSError, SubprocessError)` and returns None. `_inspect_git` (:453-472) treats None as "no alias", so the command is allowed.
    - **Proposed:** when the guard is active and an alias cannot be read, block with a visible "cannot inspect" reason. The guard has no configuration, so Lake must confirm fail-closed (Needs Lake item 4).
    - Caller: `tools/terminal_tool_guards.py:279-285`.
  - **(b) Tirith scan.** `tools/tirith_security.py:539-542` does not catch `RuntimeError` from reader start, and `tools/approval.py:662` catches only `ImportError`. A refused start therefore escapes as an exception instead of `_crash(fail_open)`.
    - **Property:** it maps to `_crash(fail_open, …)`.
  - **(c) Cosign.** `tools/tirith_security.py:228-246`. The documented policy is at :250-251: "only an explicit rejection aborts; broken cosign means SHA-256 only".
    - **Property:** a refused start returns None (that policy). It must not escape and abort the install differently.
- **Acceptance:**
  - Per check, fake the spawn to raise `OSError`, `RuntimeError`, then a timeout.
  - (a) blocks.
  - (b) with fail_open=False blocks, and with fail_open=True allows. Each counts once toward the breaker.
  - (c) returns None and the install proceeds with SHA-256 only.
- **Depends on:** C1.0, C1.S3, C5.
- **Status:** OPEN.

### C7: A37, MCP stderr sink settled when a thread fails to start

- **Property.** If the writer or drain thread of `StderrSink` fails to start, `open_stderr_sink` raises after undoing everything:
  - no `_sinks[scope]` entry for that sink;
  - both pipe fds closed;
  - `sinks_released(scope)` is True, so profile rename and delete are allowed;
  - `close_sinks` does not raise.
  
  A drain-start failure after the writer started also stops the writer. The MCP connect path at `tools/mcp_tool_transport.py:327-350` reports a connect error.
- **Code:** `tools/mcp_tool_stderr.py`:
  - `__init__` :87-108 (register :101-102 before the starts at :104-108);
  - `close` :115-122 (joins an unstarted thread);
  - `_write_loop` finally :185-191;
  - `close_sinks` :194-204;
  - `sinks_released` :207-211;
  - `open_stderr_sink` :214-221.
  
  Caller: `mcp_tool_transport.py:327`, which sits outside the `try` at :328.
- **Acceptance:**
  - The ported strict xfail `test_a_writer_that_never_starts_leaves_no_registration` passes as a plain test.
  - Variant: a drain-start failure leaves no registration, the writer has exited, and the fd count is back to its starting value.
  - `close_sinks(scope)` is callable afterwards without raising.
- **Depends on:** C1.S7 (bounded start).
- **Status:** OPEN.

### C8: A38, disposition

- **Premise rejected.** A38's "the pending turn is never settled on the normal path" requires the host to send a generic `error` frame carrying a turn's request id. The producer never does:
  - turn failures always use `turn.error` (`tui_gateway/compute_host.py:186-194, :281, :301, :345`);
  - generic `error` comes only from an unknown frame type (:182-184), a non-turn handler exception (:196-197), and invalid JSON or a non-object frame (:723, :726, with no request id).
  
  Evidence test: the harness's `test_every_reply_the_producer_emits_is_classified_the_same_by_table_and_decoder`. That test is on the parked branch, so C8.1 ports it as a plain producer test.
- **C8.1 Missing diagnostic.** A generic `error` with a request id and no waiter is dropped with no log: `host_supervisor.py:539-540`, then `_deliver_control_frame` :407-417. An `error` with no request id, and `turn.started`, fall through the `elif` chain at :541-559, which has no `else`. That is also silent.
  - **Property:** every unrouted frame is logged at warning with type and request id, rate-limited.
  - **Acceptance:** feed each unrouted frame kind to `_handle_host_frame`. There is exactly one warning per kind in the window, and no pending state changes.
  - **Status:** OPEN.
- **C8.2 Settlement gap found while checking.** Once the host sets `_closed` its reader stops (`compute_host.py:718, :737-738`), and later `turn.start` frames get no reply. They are settled only by the supervisor's exit watcher, and not when `_closing` is set (`host_supervisor.py:607-608`). This is owned by H1.

### C9: Outstanding cleanup has an owner and a bound (P-OWN)

- **Property.** Every cleanup that outlives the operation that started it has three things:
  - a named owner that finishes it or reports it;
  - a process-wide cap;
  - a defined refusal when the cap is reached: the next admission of that kind returns a structured capacity error promptly, never a hang or a traceback (AG-d7-R3).
  
  A cleanup callback that raises stays owned, not marked done (CX-d5-F3). Totals are bounded, not just each sweep (CX-d6-F1, CX-d3-5).
- **Cleanups in scope at the tip:**
  - reader threads left after a timeout by `_PipeReaders` (`_subprocess_compat.py:563-641`) and by converted C1 sites;
  - trees whose kill failed, and jobs whose close failed (`processes.py:96-112`);
  - late control handlers (`host_supervisor.py:50-51, :397-405`: TTL 1800 s and cap 64 exist; H8 settles evictions);
  - background-process readers (`process_registry.py`).
- **Mechanism (small and explicit).** A counter per kind in `_subprocess_compat`, with a cap (proposed 64), incremented at abandonment and decremented when the thread or process is observed finished. When the cap is reached, the next bounded spawn of that kind refuses. The count and cap are logged at warning when first exceeded.
- **Acceptance:**
  1. A child that never exits and whose kill is forced to fail: after N = cap abandoned calls, call N+1 returns the capacity error in < 100 ms.
  2. When the children are released, the counter returns to 0 and calls succeed again.
  3. A cleanup callback that raises leaves its entry counted.
  4. The terminal tool returns a structured error for the capacity refusal.
- **Depends on:** C1.0.
- **Status:** OPEN. Needs Lake: the cap values and refusal wording (item 5).

### H: compute host (`tui_gateway/host_supervisor.py`, `tui_gateway/compute_host_bridge.py`)

**The A25 ownership contract.** Every H row must conform to it (stated by the A25 agent, 2026-09-28; branch `magi/track-a25-a19b` at `9f133723bd`):
- **(A) One claim per turn.** A turn holds one claim from acceptance to settlement, and every start, dispatch or release checks that claim by identity.
  - On the host, `turn.start` counts as the start hand-off (`_start_turn_claim`).
  - Host completion releases only the claim that turn was dispatched under.
- **(B) Every cancellation is registered.** Each in-process cancellation stays registered until it has fully propagated, and is tied to the turn it was chosen for (`_open_cancellation(turn_epoch=...)`). A Stop revokes only the claim it observed, and only if that claim has not started.
- **(C) Only the current owner acts on the conversation.** Anything that affects the whole stored conversation needs positive, current, per-profile ownership. It happens while the lease is held, and the lease is released last.

A25 will check rows H1–H12 against this contract at the start of its round 10. H9 and I1 moved to A25.

A25 owns the turn-ownership contract, and these rows conform to it. None of the H rows changes the turn token: `turn_id = request_id = uuid4` at `compute_host_bridge.py:299`.

**Reachability note.** `HostSupervisor.shutdown()` has no production caller today; only tests call it. The server's atexit (`server.py:351-359, :384`) does not touch the supervisor. H1 and H2 are therefore latent code-shape defects (Needs Lake item 1).

- **H1 Shutdown and admission.**
  - **Property:** once `shutdown()` begins:
    - (1) `start()` never reopens the supervisor (no `_closing = False` reset) and never spawns;
    - (2) `submit_turn`, `control`, `respond`, `interrupt` and `reload_mcp` are refused under the supervisor lock with a terminal shutdown error;
    - (3) claims pending when the host exits during shutdown are settled exactly once (as the non-closing exit path already does);
    - (4) no frame is written after the shutdown frame.
  - **Code:** `start` :246-257 (fast return :253-254, reset :255); `shutdown` :259-274; `submit_turn` :300-321; `_send_frame` :500-511; `_wait_for_exit` early return :607-608, settle block :609-635. Host side: `compute_host.py:735-736` `os._exit(0)`.
  - **Acceptance (event-ordered, fake host):**
    - shutdown is waiting in `proc.wait` and a concurrent `submit_turn` is refused with the terminal error, with no frame written;
    - the host exits, and every pending turn gets one `turn.error` and every control waiter one `control.error`;
    - `start()` after shutdown raises.
  - **Depends on:** none. **Status:** OPEN.
- **H2 Refusal classification through the bridge and prompt.**
  - **Property:**
    - **Inline fallback** in `methods_prompt.py:690-726` happens only on a synchronous refusal where the frame was certainly not written (latched, spawn/hello failure, not running).
    - **A shutdown refusal is terminal:** an error to the client, with no inline run.
    - **An indeterminate send is terminal** (H6).
    - **Fallback under the lock.** The inline fallback admission is decided under the supervisor lock against the shutdown state, not by a lock-free recheck (CX-d13-F06).
  - **Code:**
    - `compute_host_bridge.py:322-334`: any exception becomes 5019. No refusal type exists; `HostShuttingDown` is absent.
    - `methods_prompt.py:690-705, :709`;
    - `compute_host_bridge.py:348-362` `_adopt_host_session_inline`.
  - **Acceptance:** fake the supervisor raising each refusal kind. Latched → inline runs once. Shutdown → error, no inline. Indeterminate → error, no inline, and the session stays pinned until the host settles.
  - **Depends on:** H1, H6. **Status:** OPEN.
- **H3 `submit_turn` false success and duplicate id.**
  - **Property:**
    - `submit_turn` returns success only if its frame was fully written and its claim is registered;
    - a duplicate request id is refused, not overwritten;
    - the failure path removes only its own `_pending_turns` / `_request_hosts` entries (compare by identity).
  - **Code:** :306-320 (overwrite :307; unconditional pops :311-312; unclaimed returns success at :313-316); ownership default in `_wait_for_exit` :620-623.
  - **Acceptance:**
    - the exit watcher settles an unrecorded rid while `_send_frame` is raising, and `submit_turn` raises (the bridge does not report `streaming`) with exactly one completion;
    - a second submit with a live id is refused, and the first `on_complete` still fires.
  - **Depends on:** none. **Status:** OPEN.
- **H4 Control, respond and interrupt sends, and reload error acks.**
  - **Property:**
    - every send boundary applies H1's gate;
    - `reload.mcp` reports failure for `control.error`, a generic `error`, a host-exit `control.error`, or a `reload_mcp.ack` whose inner response carries a JSON-RPC error.
  - **Code:**
    - `host_supervisor.py:323-391`;
    - `tui_gateway/methods_tools.py:284-290` returns `"reloaded"` for any ack;
    - compare `methods_session.py:1850-1854, :1890`.
  - **Acceptance:** each of the four error acks makes `reload.mcp` return an error response; a genuine ack returns `reloaded`.
  - **Depends on:** H1. **Status:** OPEN.
- **H5 Control request-id collision.**
  - **Property:** control, respond and interrupt claims are keyed by a supervisor-minted unique id. The client's JSON-RPC id is used only for correlation back to that client. Turn ids are unchanged, so turn-targeted interrupt keeps matching (CX-d12-F1). A reply of the wrong kind cannot settle a claim of another kind (TF-OR2-2).
  - **Code:**
    - `methods_tools.py:287` `reload-mcp-{rid}`;
    - `methods_session.py:2123` `interrupt-{rid}`;
    - `_await_reply` :338 overwrite, :348 and :360 pop by key;
    - `_send_frame` :508;
    - `_CONTROL_REPLY_TYPES` :53-55.
  - **Acceptance:** two connections send `reload.mcp` with id 7 concurrently, and each gets its own ack. A timeout of one does not remove the other's waiter. A soft interrupt with `turn_id` still applies to its turn.
  - **Depends on:** none. **Status:** OPEN.
- **H6 Uncertain send: never a second execution.**
  - **Property:** if the turn frame may have reached the host (the exception comes after `write` began), the turn is not run inline. The session stays owned by the host turn until `turn.end`, `turn.error` or host exit settles it, and the client receives a terminal error only if it settles as failed.
  - **Code:**
    - `host_supervisor.py:505-506` (the write and flush of a line-buffered text pipe, :444-447);
    - :309-320 `send_failed` completion;
    - `compute_host_bridge.py:314` (ignores `send_failed`) and :322-334 rollback;
    - `methods_prompt.py:692-726` inline.
  - **Acceptance:** a fake pipe accepts the whole line and then raises on flush. The fake host runs the turn once, the parent never enters the inline path, and one completion is observed.
  - **Depends on:** H2. **Status:** OPEN.
- **H7 Start lock hold.**
  - **Property:** a `start()` in progress (orphan reconcile ~10 s, spawn, hello 10 s, failure cleanup ~12 s) does not delay the settlement of an older host's pending claims or other hosts' frame delivery by more than a bound (proposed 1 s).
  - **Code:** `start` holds the RLock across :247-257 and the hello wait at :457; `_deliver_control_frame` :407; `_wait_for_exit` settle :609.
  - **Acceptance:** a fake hello that takes 10 s while an old host's exit watcher runs. The old claims settle in < 1 s.
  - **Depends on:** H1. **Status:** OPEN.
- **H8 Late-handler eviction.**
  - **Property:** the late-handler bound stays (TTL 1800 s, cap 64). An evicted handler (TTL or cap) is settled with an error, so the client's pending compression notice resolves. The heartbeat is not relied on to bound a wedged handler (CX-d17-F21).
  - **Code:** `host_supervisor.py:50-51, :397-405`. Registrants: `methods_session.py:1881`, `methods_slash.py:377`.
  - **Acceptance:** register 65 handlers. The oldest one's callback receives an eviction error. With the TTL advanced, pruning calls each expired handler once.
  - **Depends on:** C9. **Status:** OPEN.
- **H9 Host control head-of-line blocking (new, found by the code-path read).**
  - **Property:** a long control (compress, reload) for session A does not delay `interrupt`, `respond` or `turn.start` for other sessions, or Stop for A, beyond a bound.
  - **Code:** `compute_host.py:731` (the frame is handled on the stdin reader thread); `_handle_control` :497-530; compress runs inline via `methods_session.py:1952-1974`; `reload_mcp` :489-495; the separate heartbeat thread :564-571 is why the watchdog never fires.
  - **Acceptance:** a fake compress blocks for 30 s. An `interrupt` for another session is applied in < 1 s, and a `respond` is delivered within the parent's 15 s wait.
  - **Depends on:** I1 (A25 owns Stop ordering). **Status:** OPEN. Needs Lake (item 3).
- **H10 Crash latch: pin today's behaviour (no code change).**
  - **Property (as the code behaves):**
    - the latch trips at the (max+1)-th exit in the window;
    - an exit while a live replacement exists is not counted;
    - every exit reason counts;
    - while latched no host can spawn (`_spawn_locked` :419-424), so nothing re-latches;
    - expiry happens only in `start()` after 300 s and clears the history;
    - the respawn thread calls `_spawn_locked` directly and checks `_closing`.
  - **Code:** `host_supervisor.py:246-252, :673-699`.
  - **Acceptance:** a plain regression test, adapted from the harness's `test_host_supervisor_crash_latch_sm.py` without the `_sm` seams, asserts each clause.
  - **Depends on:** none. **Status:** OPEN.
- **H11 Response relay reports failure.**
  - **Property:** if `respond()` fails, `_relay_compute_host_response` does not pop `_compute_host_open_request` and does not report the frame as delivered. The client receives an error and the failure is logged at warning.
  - **Code:** `compute_host_bridge.py:177-190`: it pops at :185-186, catches `Exception` and returns True at :187-190. Callers: `methods_prompt.py:1206`, `rpc_dispatch.py:57`.
  - **Acceptance:** fake `respond()` raising. The client receives an error response, and the open request is still recorded.
  - **Depends on:** H1. **Status:** OPEN.
- **H12 Windows orphan host never reconciled (new, found by the code-path read; reproduce first).**
  - **Property:** on Windows, `reconcile_startup_orphan` identifies and terminates a stale compute host from the previous backend.
  - **Code:** `host_supervisor.py:136-143` `_pid_command` falls back to `ps`, which cannot answer on Windows (see `compute_host.py:670-671`). `is_compute_host_identity` is therefore False, and reconcile returns "pid-reuse-ignored" (:295, :703-712).
  - **Acceptance:** a contained test that starts a real host, detaches it from the supervisor, and starts a new supervisor. The old host is terminated. If the reproduction fails, the row closes as disproved with that evidence.
  - **Depends on:** C2.3. **Status:** OPEN.

### I: integration

- **I1 A25 cancellation ownership at the host boundary.**
  - **Property:**
    - a Stop or interrupt affects only the turn it targeted;
    - a Stop issued after dispatch, before the host claims the turn, cancels that turn;
    - a Stop sent to a respawned host whose session does not exist yet is not lost silently.
  - **Code:**
    - parent: `session_lifecycle.py:603-649` (`interrupt(sid, request_id=f"interrupt-{rid}")`, no `turn_id`, no ack check, :612-613); `session_auto_continue.py:210-235, :285-292`;
    - host: `compute_host.py:238-253`, and the claim :294-305, which resets the cancel flag at :303;
    - direct `prompt.submit` frames carry no `queued_prompt_generation` (`methods_prompt.py:690-691`).
  - **Defect found:** a Stop can overtake a queued `turn.start` inside the host and be lost. `interrupt` runs inline on the reader thread, and `turn.start` goes onto the executor at :215.
  - **Also covers:** CX-d13-F07 (the interrupt's effect is not bound to its turn).
  - **Acceptance:**
    - event-ordered: a Stop is delivered between the host's receipt of `turn.start` and its claim, and the turn ends `interrupted` without calling the model;
    - a soft interrupt carrying an old `turn_id` is not applied to the next turn;
    - A25's own census and probes pass on the combined branch.
  - **Depends on:** A25 (IN REVIEW round 9) merged, H5 and H9. **Status:** OPEN.
- **I2 Combined release candidate.**
  - **Property:** every Part 1 row merged on one branch from `magi/sync-v2026.9.21`, together with A25.
  - **Acceptance:**
    1. The full suite through the isolated runner. Failures equal baseline plus written dispositions, per the gate's closure rule.
    2. All C/H/I acceptance tests pass on Windows.
    3. Commit-pressure reproduction of the original exposure: the backend runs in a job with a small commit limit so thread creation fails. A live-session smoke run (prompt turn, terminal command, @-completion, MCP connect, compute-host turn and Stop, Tirith check) returns errors within their bounds, leaves no stray children, and the backend stays responsive.
    4. The framework-absence checks in "Baseline and rules" return 0 on the candidate.
    5. `git diff --check` is clean (AG-d1-G4).
    6. Contained real-process tests are run, not mocked kernel handles (AG-d13-O4).
  - **Depends on:** all rows. **Status:** OPEN.

## Needs Lake

**Lake's rulings, 2026-09-28:**
- **1. Accepted:** fix H1/H2 even though they are latent.
- **2. Accepted:** the proposed A34 scope, with per-site evidence for each exclusion.
- **3.** H9 and I1 move to **A25** as new sub-items. A25 owns turn and cancellation ownership. A28-Core conforms to A25 and does not fix them itself.
- **4. Accepted:** the self-repo guard fails closed with a visible reason.
- **5. Accepted:** a cap of 64 per kind, with a structured capacity error.
- **6. Accepted:** the Tirith breaker is keyed by resolved binary path.
- **7. Deferred by Lake:** keep today's inline fallback when the host is latched or unavailable. Remaining behaviour: during the 300 s latch cooldown, turns run in the gateway process without isolation. This becomes a post-deploy item.
- **8. Accepted:** reachability is re-traced at the tip as part of each slice's acceptance.

Before scope is frozen, Codex and Antigravity each do one exhaustive review of this list.

The proposals as originally drafted follow.

1. **H1/H2 are latent.** `HostSupervisor.shutdown()` has no production caller. Proposed: fix them anyway (both are small, and H2's classification is shared with H6). The alternative is to defer H1/H2 and state that the remaining behaviour is "shutdown() is test-only; if it is ever called, a concurrent submit can be lost and `start()` reopens the supervisor".
2. **A34 scale (C3).** There are 338 hot-path hits.
   - **Proposed:** fix the named hot-path pools and thread starts plus every partial-start leak; exclude `to_thread` sites whose awaiting request fails cleanly, with per-site evidence.
   - **Remaining behaviour if excluded:** under commit pressure those requests fail or stall rather than leak.
3. **Two new host defects found by the code-path read, outside the listed items: H9 (head-of-line blocking) and I1 (a Stop overtaking `turn.start`).** Both are in A25's area.
   - **Proposed:** keep both in A28-Core, conforming to A25; or route both to A25 as new sub-items.
   - **Remaining behaviour if deferred:** during a long compress, Stop and approval answers for every session in that host are delayed; a Stop racing a direct submit can be lost.
4. **Self-repo guard fail policy (C6a).** The guard has no configuration. Proposed: fail-closed, meaning "cannot inspect" is blocked with a visible reason.
5. **C9 cap values and refusal wording.** The proposal is a cap of 64 per kind and a structured capacity error.
6. **Tirith breaker is process-global** while the binary is resolved per profile (`tirith_security.py:67, :118-125`). One profile's broken binary opens the breaker for every served profile, and after C5 that means every fail-closed profile blocks.
   - **Proposed:** key the breaker by resolved path in C5.
   - **Deferral:** state that behaviour as accepted.
7. **CX-d11-F5 / AG-d11-F3 (Part 2).** A latched or unavailable host runs turns inline even when isolation is configured.
   - **Proposed deferral:** keep today's fallback on a synchronous, certainly-unsent refusal.
   - **Remaining behaviour:** during the 300 s latch cooldown, turns run in the gateway process without isolation.
8. **The census reachability for 143 sites was not re-traced** (it was taken from r2 at `b6827ff89f`). C1 acceptance (g) does this per slice. It is listed here because TOOLING and POSIX-only exclusions rest on it.

## Part 2: disposition of every existing A28b finding

**Outcomes:**
- **CORE**: fixed-in-core; the named Part 1 row owns it.
- **FW**: framework-only. The component is absent from the release candidate, confirmed by the checks in "Baseline and rules". The component names used are:
  - *tables* = the r13–r18 state tables and `tests/sm_harness/tables/`;
  - *harness* = `tests/sm_harness/*` (explorer, loader, recorder, model, corpus);
  - *runner* = `hermes_cli/subprocess_bounded*`;
  - *census/guard* = `scripts/ci/check_subprocess_sites` and the runtime site guard (`live_entry`);
  - *diagnostics* = the `state/subprocess-capacity` publisher and doctor reader;
  - *launch/entry* = the launch classifier and entry-point registration hook;
  - *seams* = `hermes_cli/_sm.py` guarded containers;
  - *design machinery* = r18's scope, slot, ledger, lease, outbox, writer-queue, attempt and transfer state machines.
- **DIS**: disproved or unreachable at the tip, with evidence.
- **LAKE**: needs Lake.

**Line numbers** without a file are `tui_gateway/host_supervisor.py` at the tip.

### Codex

| id | finding | outcome | evidence / row |
|---|---|---|---|
| CX-d1-F1 | keep communicate timeout-and-retry separate from run cleanup (cron polling) | CORE | C1.S8 (CRON-1 poll semantics) |
| CX-d1-F2 | own-thread partial-start cleanup (quiet_single_query, codex_app_server, tui server) | CORE | C1.S6, C1.S8, C4 |
| CX-d1-F3 | job containment at spawn, not retrofitted | CORE | C1 property 1, C1.0 |
| CX-d1-F4 | frame guard is not a sound no-bypass check | FW | census/guard |
| CX-d1-F5 | classification and totals wrong | CORE | C1 classification re-derived at tip; acceptance (g) |
| CX-d2-F1 | define the cleanup budget and what happens when it runs out (A33) | CORE | C2.1, C9 |
| CX-d2-F2 | owner API misses pre-spawn and composite owners (MCP sink, SSH pair) | CORE | C1.S7, C7 |
| CX-d2-F3 | guard cannot detect inventory errors using the same inventory | FW | census/guard; core substitute is C1 (g) |
| CX-d3-1 | pipe ownership across the foreground-to-registry handoff | CORE | C1.S5 (`adopt_local` :1202) |
| CX-d3-2 | keep compute-host kill-on-close; serialize an in-flight spawn | DIS | Holds at the tip: `spawn_server` kill-on-close `host_supervisor.py:444`; start/spawn/hello serialized under `_lock` :247-257, :457 |
| CX-d3-3 | cleanup keeps what it abandons (job handles, callbacks) | CORE | C9 |
| CX-d3-4 | keep A3 output-bound drains and T13 fresh-context contract | CORE | C1.S1 (A3), C4 (T13) |
| CX-d3-5 | bound the abandoned sweep; wall-time formula | CORE | C9 (total bound), C2.1 |
| CX-d3-6 | census key and platform exemptions vs promised coverage | CORE | C1 classification (POSIX-only with reason; P8 kept) |
| CX-d4-F1 | publishing the spawn ends reservation before ready | DIS | At the tip, `start()` holds the lock through the hello wait (:247-257, :457); no joiner sees a pre-hello host |
| CX-d4-F2 | provisional registry session visible before transfer commits | CORE | C1.S5 acceptance 3 (visible only once committed; failure withdraws) |
| CX-d4-F3 | stop observation needs an irreversible claim before relinquishment | CORE | C1.S5 acceptance 3 (Stop during handoff honoured) |
| CX-d4-F4 | transfer must carry incremental decoder state | CORE | C1.S5 acceptance 3 (split multibyte) |
| CX-d5-F1 | an attempt leaving STARTING is not final | DIS | No attempt state at the tip: start is synchronous under the lock and returns only after hello or cleanup (:247-257, :461-469) |
| CX-d5-F2 | cancellation can win after validation | FW | runner (cancellation token) |
| CX-d5-F3 | a raising cleanup callback is marked done | CORE | C9 acceptance 3 |
| CX-d6-F1 | per-sweep bound does not bound total retained cleanup | CORE | C9 |
| CX-d7-F1 | security guards' OSError paths miss the refusal policy (Tirith, self-repo) | CORE | C6 |
| CX-d7-F2 | CLI doctor healthy while cleanup failed | FW | diagnostics; C9 logs at warning instead |
| CX-d7-F3 | retry bound vs sweep allowance | FW | design machinery (sweep) |
| CX-d8-F1 | catching before `_crash` does not cover the expired (half-open) breaker | CORE | C5 |
| CX-d8-F2 | old healthy snapshot hides failed cleanup | FW | diagnostics |
| CX-d8-F3 | round-robin cursor bound | FW | design machinery (sweep) |
| CX-d9-F1 | shutdown does not revoke READY fast path or send boundary | CORE | H1 |
| CX-d9-F2 | FIFO does not close worker-finished vs park | FW | design machinery (worker cell) |
| CX-d9-F3 | failed conditional registry removal strands attempt | DIS | `_remove_registry` never raises (:494-498); start failure fully cleaned (:461-469) |
| CX-d9-F4 | entry-point table misses `hermes mcp serve` | FW | launch/entry |
| CX-d10-F1 | `HostShuttingDown` not terminal through the caller (inline fallback) | CORE | H2 |
| CX-d10-F2 | single last-pass marker across sweepers | FW | design machinery (sweep) |
| CX-d10-F3 | fast interactive chat bypasses registration hook | FW | launch/entry |
| CX-d11-F1 | terminal shutdown through revocation, join timeout, supervisor except | CORE | H2, H3 |
| CX-d11-F2 | send revocation for controls/responses; reload error frame | CORE | H4 |
| CX-d11-F3 | duplicate-ID sweep misses the control collision | CORE | H5 |
| CX-d11-F4 | two resident launch transitions classified one-shot | FW | launch/entry |
| CX-d11-F5 | isolation rule vs compute-host capacity/latch fallback | LAKE | Needs Lake 7 |
| CX-d12-F1 | fresh request IDs break turn-targeted interrupt | CORE | H5 constraint (turn ids unchanged; acceptance) |
| CX-d12-F2 | join-result arrivals reach impossible cells | FW | tables |
| CX-d12-F3 | failed teardown/shutdown misclassified | FW | tables |
| CX-d12-F4 | request-host map has two schemas | FW | tables |
| CX-d13-F01 | acquisition finishes after scope settled and released slot | FW | design machinery (scope/slot) |
| CX-d13-F02 | close releases capacity before release succeeds; disposes a borrowed stream | FW | design machinery (slot/lease) |
| CX-d13-F03 | relinquishment after the finished latch | FW | design machinery |
| CX-d13-F04 | cancelled handoff rolls back into impossible states | FW | design machinery (transfer) |
| CX-d13-F05 | sink table loses the stop signal when the drain never starts | CORE | C7 (drain-start failure variant) |
| CX-d13-F06 | lock-free shutdown recheck is not an inline-admission boundary | CORE | H2 (decided under the lock) |
| CX-d13-F07 | matching turn ID at lookup does not bind the interrupt's effects | CORE | I1 |
| CX-d13-F08 | indeterminate send still classified as safe inline fallback | CORE | H6 |
| CX-d13-F09 | normal losing worker races reach impossible cells | FW | tables |
| CX-d13-F10 | T4 omits counter updates | FW | tables |
| CX-d13-F11 | gated reader lease acquisition | FW | design machinery (lease) |
| CX-d13-F12 | dependency registration and immediate cleanup vanished | FW | tables |
| CX-d13-F13 | breaker table cannot settle scans begun in an earlier state | CORE | C5 (A39 stale completion) |
| CX-d13-F14 | J has no row for a dead child before send_open clears | FW | tables |
| CX-d13-F15 | J returns shutdown before teardown | FW | tables |
| CX-d13-F16 | T13 incomplete supervisor/attempt lifecycle | FW | tables |
| CX-d13-F17 | C conflates send, progress, timeout, completion | FW | tables |
| CX-d13-F18 | communication has no partial-start / start-once claim | CORE | C1 common acceptance (a) |
| CX-d13-F19 | job table skips partial construction, assign, resume failure | DIS | Production failure path is bounded and complete: `processes.py:184-200` kills, `wait(10)`, closes job, streams and handle |
| CX-d13-F20 | lock rules conflict with operations under those locks | FW | tables |
| CX-d13-F21 | emissions do not type-check | FW | tables |
| CX-d13-F22 | T15 misclassifies flag combinations | FW | tables |
| CX-d13-F23 | "impossible" is circular | FW | harness |
| CX-d13-F24 | disjoint lock sets not a sound POR | FW | harness |
| CX-d13-F25 | exhaustive search vs real-process CI budget | FW | harness |
| CX-d13-F26 | a seed cannot replay OS timing | FW | harness |
| CX-d13-F27 | schema omits fields to tell failing states apart | FW | harness |
| CX-d13-F28 | prose and test assertions conflict with cells | FW | tables |
| CX-d14-F01 | gated-reader reservation erases transfer state | FW | design machinery |
| CX-d14-F02 | reply/shutdown settles a request before the sender reports the write | CORE | H3 (false success while the exit watcher settles) |
| CX-d14-F03 | handoff/late rollback bypasses accounting | FW | design machinery (ledger) |
| CX-d14-F04 | scope-to-token emission vs lock order | FW | tables |
| CX-d14-F05 | outbox has no delivery owner | FW | design machinery (outbox) |
| CX-d14-F06 | request model lost identity schema and duplicate refusal | CORE | H3 (duplicate refused), H5 |
| CX-d14-F07 | exception before handoff commit leaves no rollback | CORE | C1.S5 acceptance 3 (failed handoff withdraws) |
| CX-d14-F08 | foreground cleanup publishes settle while slot LIVE | FW | design machinery (ledger) |
| CX-d14-F09 | communication can start workers but never enter ACTIVE | FW | tables |
| CX-d14-F10 | partial job construction obligations impossible | FW | tables |
| CX-d14-F11 | terminal frames classified as progress | FW | tables |
| CX-d14-F12 | indeterminate sends: undefined continuation and caller ownership | CORE | H6 |
| CX-d14-F13 | fresh startup no success branch; registry failure no teardown | DIS | `_spawn_locked` failure path detaches, terminates the tree and removes the registry (:461-469); `_remove_registry` cannot raise (:494-498) |
| CX-d14-F14 | old backoff timer can clear a newer latch | DIS | The respawn thread re-checks `_stopped_respawning` under the lock (:692-694); the latch clears only in `start()` after cooldown (:248-252) |
| CX-d14-F15 | canonical emissions do not compile; root/reap unmodelled | FW | tables |
| CX-d14-F16 | R0 omits locks | FW | tables |
| CX-d14-F17 | T8 omits health/discovery decisions | FW | diagnostics |
| CX-d14-F18 | import-time equality misses joint omission | FW | harness |
| CX-d14-F19 | state equality does not verify effects | FW | harness |
| CX-d14-F20 | inline claims have no settlement lifecycle | FW | design machinery (inline claims); production inline path is H2/H6 |
| CX-d14-F21 | "self-contained" contract imports superseded policy | FW | tables |
| CX-d14-F22 | R1 refuses the cleanup-helper acquisition abort needs | FW | design machinery |
| CX-d15-F01 | host-death uncertainty leaves the bridge waiting | CORE | H1 (the exit during `_closing` settles pending) |
| CX-d15-F02 | shutdown settles requests but queued frames stay executable | DIS | Host calls `os._exit(0)` on reading `shutdown` (`compute_host.py:735-736`); no later frame executes. The residual lost-submit is H1 |
| CX-d15-F03 | polling a dead host before its watcher strands claims | DIS | Settlement is per owner via `_request_hosts` / `current` in the old host's watcher (:609-635); a replacement does not strand them |
| CX-d15-F04 | after-emissions in `finally` can be lost | FW | design machinery |
| CX-d15-F05 | writer queue locking / send-claim boundary | FW | design machinery (writer thread) |
| CX-d15-F06 | removing inline claims removes cancellation handoff | FW | design machinery; production cancellation is I1 |
| CX-d15-F07 | READY teardown has no executing owner | FW | tables |
| CX-d15-F08 | late acquisition does not wake a parked entry | FW | design machinery |
| CX-d15-F09 | lost handoff completion reaches impossible scope cell | FW | tables |
| CX-d15-F10 | T16 no failed-kill retry or abort-to-reap | FW | tables; the production residue is C9 |
| CX-d15-F11 | E4 timeout cleanup delegates to an unbounded stdlib wait | CORE | C2.1, C1.0 |
| CX-d15-F12 | every successful hello resets the crash counter | DIS | Hello never touches `_restart_times`; it is cleared only on latch expiry in `start()` (:248-252) |
| CX-d15-F13 | Field descriptors do not enforce instrumentation | FW | seams |
| CX-d15-F14 | emission vocabulary fails target validation | FW | tables |
| CX-d15-F15 | import boundary drops idle recovery and site contracts | FW | tables |
| CX-d15-F16 | communication reports success before the input worker's result | CORE | C1 common acceptance (d) |
| CX-d15-F17 | worker release assumes a borrow never made | FW | design machinery (lease) |
| CX-d15-F18 | refusal at the observation tick excluded from health | FW | diagnostics |
| CX-d15-F19 | invariants and corpus name removed mechanisms | FW | harness |
| CX-d17-F01 | DPOR loses executions when an actor becomes disabled | FW | harness |
| CX-d17-F02 | DPOR does not preserve H7 | FW | harness |
| CX-d17-F03 | recorder can choose a convenient before-state | FW | harness |
| CX-d17-F04 | H4 ignores conditional-emission obligations | FW | harness |
| CX-d17-F05 | effect recording can certify a release that did not happen | FW | harness |
| CX-d17-F06 | H7 accepts missing owners | FW | harness |
| CX-d17-F07 | scope admission publishes before the ledger accepts | FW | design machinery |
| CX-d17-F08 | scope releases capacity before lease disposal | FW | design machinery |
| CX-d17-F09 | guarded containers have mutation bypasses | FW | seams |
| CX-d17-F10 | H2 cannot discover joint omissions | FW | harness |
| CX-d17-F11 | non-Boolean guards as opaque Booleans | FW | harness |
| CX-d17-F12 | loader accepts invalid assignments | FW | harness |
| CX-d17-F13 | lock validation hides cross-machine violations | FW | harness |
| CX-d17-F14 | model drops targets and skips effects | FW | harness |
| CX-d17-F15 | corpus PASS without exercising witnesses | FW | harness |
| CX-d17-F16 | replay accepts disabled steps | FW | harness |
| CX-d17-F17 | PENDING worker gates overwrite CAS winner | FW | tables |
| CX-d17-F18 | hello transcription drops admissible/alive | FW | tables |
| CX-d17-F19 | attempt-local closing copy loses shutdown | FW | tables; the production shutdown behaviour is H1 |
| CX-d17-F20 | watcher crashes hardcode `replacement_alive=False` | FW | tables; production computes it under the lock (:676-677), pinned by H10 |
| CX-d17-F21 | TTL removal relies on a heartbeat the source does not give | CORE | H8 (the TTL/cap stay; eviction settles) |
| CX-d17-F22 | `after_settle` on an ABORTED worker never runnable | FW | tables |
| CX-d17-F23 | HALF_OPEN drops the stale caller's verdict | CORE | C5 acceptance 5 |
| CX-d17-F24 | crash-latch table disagrees with the code | FW | tables; code behaviour pinned by H10 |
| CX-d17-F25 | harness contract omits operational acceptance rules | FW | harness |
| CX-d17-F26 | footprints omit the reference read | FW | harness |
| CX-d17-F27 | generic-turn-error diagnostic absent | CORE | C8.1 |

### Antigravity

| id | finding | outcome | evidence / row |
|---|---|---|---|
| AG-d1-R1 | author runner in new `subprocess_bounded.py` | FW | runner; core extends `_subprocess_compat` (C1.0) |
| AG-d1-R2 | job without kill-on-close + TerminateJobObject | CORE | C1 property 1 (`spawn_command`, `processes.py:115-145`) |
| AG-d1-R3 | fault-injection test per own-thread site | CORE | C1 common acceptance (a)/(b) |
| AG-d1-R4 | `ReaderStartError(OSError)` acceptable | CORE | C1 property 2 (OSError subclass) |
| AG-d1-G1 | runner lives in its own module | FW | runner |
| AG-d1-G2 | stdin writer own thread (>64 KB) | CORE | C1 acceptance (d) |
| AG-d1-G3 | TimeoutExpired carries partial output | CORE | C1 acceptance (e) |
| AG-d1-G4 | `git diff --check` passes | CORE | I2 acceptance 5 |
| AG-d1-O1 | ~35 sites pass pipes via kwargs | CORE | C1 census includes them (TUI-7/8, TUI-9, D16, C21, T27) |
| AG-d3-A1 | census key collides per function | FW | census/guard |
| AG-d3-A2 | lambdas share a qualname | FW | census/guard |
| AG-d3-A3 | guard stack walk sees the wrapper | FW | census/guard |
| AG-d3-O1 | real partial-start leaks (copilot, codex, slash worker, locked `Thread.start`, context_references :443) | CORE | C4, C1.S5, C1.S1, C1.S6 |
| AG-d4-F1 | reservation cleared before hello | DIS | Same as CX-d4-F1: lock held through hello (:247-257, :457) |
| AG-d4-R1 | defer A35 on_owner_death | CORE | Superseded by the Codex d4 ruling and gate A35; C4 |
| AG-d7-R1 | doctor scans all profiles' capacity files | FW | diagnostics |
| AG-d7-R2 | add `SubprocessCapacityError` to `_POPEN_ERRORS` | FW | runner error type (reversed by AG-d8); C1.S2 uses OSError |
| AG-d7-R3 | terminal_tool structured capacity message | CORE | C9 acceptance 4 |
| AG-d8-R1 | `acp_adapter/entry.py` diagnostic owner | FW | diagnostics |
| AG-d8-O1 | Tirith OSError opens breaker; self-repo guard OSError allows | CORE | C5, C6 |
| AG-d11-F1 | shutdown disposition misses revoked/join timeouts; except returns silently | CORE | H2, H3 |
| AG-d11-F2 | controls unrevoked; `methods_tools.py:290` "reloaded" on error | CORE | H4 |
| AG-d11-F3 | capacity fallback inline contradicts isolation | LAKE | Needs Lake 7 |
| AG-d11-F4 | `_pending_controls` overwrite on client id collision | CORE | H5 |
| AG-d11-F5 | launch misclassification (TTY `-q`, `gateway restart`) | FW | launch/entry |
| AG-d11-F6 | `_read_user_safe_directories` caches `[]` on refusal | CORE | C2.2 |
| AG-d11-F7 | `_RootCache` 30 s negative on refusal | CORE | C2.2 |
| AG-d11-O1 | `_relay_compute_host_response` returns True on exception | CORE | H11 |
| AG-d13-P1-1 | T1 `(RUNNING, claim_stop)` marked impossible | FW | tables |
| AG-d13-P1-2 | T4 clean_close omits `used -= 1` | FW | tables |
| AG-d13-P2-1 | T11 CLOSE_PENDING needs a kill fallback | FW | tables; the production residue is C9 |
| AG-d13-P2-2 | crash latch has no table | FW | tables; code pinned by H10 |
| AG-d13-P2-3 | POR by lock disjointness | FW | harness |
| AG-d13-P3-1 | T5 next state unnamed | FW | tables |
| AG-d13-P3-2 | explorer depth 4 too shallow | FW | harness |
| AG-d13-O1 | multiplex latch has no table | FW | tables (production untouched by Core) |
| AG-d13-O2 | profile scope stack has no table | FW | tables (production untouched by Core) |
| AG-d13-O3 | FIFO backpressure has no table | FW | tables |
| AG-d13-O4 | require real Windows integration tests | CORE | I2 acceptance 6 |
| AG-d14-P3-1 | R14 depends on A25 turn-token checks | CORE | I1 |
| AG-d14-P3-2 | outbox drains only at the next sweep | FW | design machinery (outbox) |
| AG-d15-P2-01 | after-emission raise skips the rest | FW | design machinery |
| AG-d15-P2-02 | E4 taskkill not capped by the remaining budget | CORE | C2.1 |
| AG-d15-P3-01 | generic error frames get the 3-map compare-and-remove | FW | tables (documentation) |
| AG-d17-F01 | DPOR prunes disabling interleavings | FW | harness |
| AG-d17-F02 | `dep_ready` only FINISHED | FW | tables |
| AG-d17-F03 | TTL removal relies on heartbeat | CORE | H8 |
| AG-d17-F04 | hello_ok lacks revoked/exited guards | FW | tables |
| AG-d17-F05 | T8 DEAD forbids in-flight write results | FW | diagnostics |
| AG-d17-F06 | T01 gate CAS overwrite | FW | tables |
| AG-d17-F07 | T06 relinquish ignores borrower identity | FW | design machinery (lease) |
| AG-d17-F08 | T13.A `replacement_alive=False`; shutdown ignored in CLOSING_DOWN | FW | tables; production is H1/H10 |
| AG-d17-F09 | `host_supervisor.py:688-699` re-arms `_latched_at` while LATCHED | DIS | At the tip, `_latched_at = now` only on the tripping exit (:679-685); while latched, `_spawn_locked` refuses (:419-424), so no host exists to crash |
| AG-d17-F10 | HALF_OPEN stale completion drops verdict | CORE | C5 acceptance 5 |
| AG-d17-F11 | `_sm.Set` allocation in `mcp_tool_stderr.py:106` | FW | seams (0 `_sm` references at the tip) |
| AG-d17-F12 | T9 releases slot before lease disposal | FW | design machinery |
| AG-d17-F13 | generic turn-id error dropped without log | CORE | C8.1 |
| AG-d17-F14 | loader treats relational guards as atoms | FW | harness |
| AG-d17-O1 | footprints from first branch only | FW | harness |
| AG-d17-O2 | DPOR drops intermediate `expect.always` | FW | harness |
| AG-d17-O3 | planted wrong next-state passes loader | FW | harness |
| AG-d17-O4 | planted missing emission undetected | FW | harness |
| AG-d17-O5 | `_restart_times` as `_sm.Queue` overhead | FW | seams |

AG-d6, AG-d9 and AG-d10 recorded no findings. AG-d13–d15 cite paths that do not exist at `c34ce0a054`, so their verification claims were not relied on here.

### Harness `TABLE-FINDINGS.md` (`magi/a28b-0b-harness`)

| id | finding | outcome | evidence / row |
|---|---|---|---|
| TF-CL-1 | latch threshold (max+1, uncounted) vs table | FW | tables (r17 follows the code); H10 pins the code |
| TF-CL-2 | exit with a live replacement not counted | FW | tables; H10 |
| TF-CL-3 | latch expiry clears history | FW | tables; H10 |
| TF-CL-4 | no generation/BACKOFF state in code | FW | tables; H10 |
| TF-T12-1 | registration row missing | FW | tables |
| TF-T12-2 | writer start failure leaves the sink registered (real bug, A37) | CORE | C7 (ported strict xfail) |
| TF-T12-3 | `writer_stop` sets only `_closed` | FW | tables (informational; code correct) |
| TF-OR2-1 | generic `error` for a turn not settled (A38) | DIS | Producer never sends it (`compute_host.py:182-197, :281, :301, :345, :723, :726`); the diagnostic is C8.1 |
| TF-OR2-2 | kind-insensitive `_CONTROL_REPLY_TYPES` settles any control claim | CORE | H5 (unique internal ids; kind checked) |
| TF-X1 | TAKEN deadline settles while the frame may run | FW | tables |
| TF-X2 | no-slot cleanup deadline | FW | design machinery |
| TF-X3 | abort lost after a pre-CAS commit loss | FW | design machinery (transfer) |
| TF-X4 | close_ok `stop_req` to a PENDING drain | FW | design machinery |
| TF-D1 | §12 not self-contained | FW | tables |
| TF-D2 | `writer_aborted` has no producer | FW | tables (the code bug is C7) |
| TF-D3 | T9 close/abort effects not emissions | FW | tables |
| TF-D4 | `process_exited` / `reaped` target T13 | FW | tables |
| TF-D5 | OS results with no emitting cell | FW | tables |
| TF-D6 | T7 completion columns vs one emission | FW | tables |
| TF-D7 | T4 merged move column; sweeper columns | FW | tables |
| TF-D8 | CL "at the maximum" ambiguous | FW | tables; H10 |
| TF-D9 | T13 `start()` rows not first-match | FW | tables |
| TF-D10 | T13.A watcher duties | FW | tables |
| TF-D11 | T13.C dequeue emitter; LATE ttl | FW | tables |
| TF-D12 | T3 hint and threshold | FW | tables |
| TF-D13 | T10 start_deadline; reader identity | FW | tables |
| TF-D14 | T9 refused admission | FW | tables |
| TF-D15 | T8 reader relation not total | FW | diagnostics |
| TF-D16 | OR-2 reload reply kinds | FW | tables (production reload acks are H4) |
| TF-D17 | T14 callers column | FW | tables |
| TF-D18 | T1 → T10 emissions unassigned | FW | tables |
| TF-S1 | schema: cells set only own fields | FW | harness |
| TF-S2 | schema: relation rules cannot fall through | FW | harness |
| TF-S3 | schema: emission payload fixed at emit | FW | harness |
| TF-S4 | schema: guards cannot compare references | FW | harness |
| TF-S5 | schema: payload field `pass` | FW | harness |
| TF-N1 | T9 OS-result emissions fix success payload | FW | tables |
| TF-N2 | close_ok `stop_req` targets | FW | tables |
| TF-N3 | CLOSING as terminate/close producer | FW | tables |
| TF-N4 | T13.A OS actions are effects | FW | tables |
| TF-N5 | `watcher_run` `replacement_alive` | FW | tables; production computes it under the lock (:676-677) |
| TF-N6 | `queue_close(exit)` when closing | FW | tables |
| TF-N7 | T13.start row 3 expansion | FW | tables |
| TF-N8 | CL window pruning not modelled | FW | tables; H10 covers pruning (:678) |
| TF-N9 | T7 OPEN_EXPIRED dropped; streak while OPEN | FW | tables; the production breaker is C5 |
| TF-N10 | T8 `dirty` reset | FW | diagnostics |
| TF-N11 | T11 CLOSE_PENDING/terminate | FW | tables |
| TF-N12 | T5 aborting with post_cas_raise | FW | tables |
| TF-N13 | T1 → T10 routing references | FW | tables |
| TF-N14 | OR2.settle drops late fields | FW | tables |
| TF-T7 | stale Tirith crash re-arms the breaker (A39 strict xfail) | CORE | C5 (ported strict xfail) |

## Counts

| source | rows | CORE | FW | DIS | LAKE |
|---|---|---|---|---|---|
| Codex d1–d17 | 140 | 40 | 89 | 10 | 1 |
| Antigravity d1–d17 | 63 | 23 | 37 | 2 | 1 |
| Harness TABLE-FINDINGS | 51 | 3 | 47 | 1 | 0 |
| **total** | **254** | **66** | **173** | **13** | **2** |

Part 1 rows, all OPEN:
- 16 C1 slices plus C1.0;
- C2.1–C2.3;
- C3 through C9, with C8 carrying one open sub-row (C8.1);
- H1–H12;
- I1 and I2.

Every demonstrated production bug has a CORE row: A37 (C7), A39 (C5), A36 (C5), F27/A38 diagnostic (C8.1), the reload error ack (H4), the control id collision (H5), the refused-probe caches (C2.2), the relay swallow (H11), uncertain-send double execution (H6), the partial-start leaks (C1.S5, C1.S6, C4) and the security fail-open paths (C6).
