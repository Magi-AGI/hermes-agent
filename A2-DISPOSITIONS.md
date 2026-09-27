# A2 dispositions — checklist A17 (track gateway-3)

Host: Windows 11 (the deploy host), Python 3.11 venv of this checkout, tests run only through
`scripts/run_tests_isolated.py -j 4`. Each item below failed identically on the base commit
(`c055201423`, sync branch). **None is a Hermes product defect**: all three are test-harness
assumptions about the host. Two are fixed in the tests themselves (they now pass on every host); one
is marked for the host it is about.

## 1. `tests/tools/test_file_ops_cwd_tracking.py::…::test_patch_returns_success_only_when_file_actually_written`

**Fixed in the test (two harness bugs); production behaved correctly.**

- **Cause 1: bare `bash` is WSL on this host.** The test's fake terminal envs ran `subprocess.run(["bash", "-c", …])`.
  On Windows `CreateProcess` searches `System32` before `PATH`, so `bash` is `C:\Windows\System32\bash.exe`, the
  WSL launcher (`shutil.which` still reports Git Bash, which hides it). WSL has no `/c/...` mount (it uses
  `/mnt/c`), so every Git-Bash-form path Hermes builds was "not found". Production never spawns a bare `bash`:
  the local backend uses `tools.environments.local._find_bash()` (Git Bash on Windows).
- **Cause 2: text-mode stdin.** The fake also piped the payload with `text=True`, which on Windows turns `\n`
  into `\r\n`. The file landed as `new content\r\n`; production's post-write verification
  (`on-disk content hash differs from the intended write`) correctly reported the write as failed. Production
  pipes stdin as UTF-8 bytes (`base_output._pipe_stdin`) precisely to avoid this.
- **Fix:** the fakes now run through `_run_bash()` in the test module, shaped like the local backend
  (`_find_bash()`, bytes stdin). 10/10 pass.

The same bare-`bash` harness bug made 14 more tests fail on this host (not in A17's list; same root cause, same
one-line fix, all now pass): `test_remote_media_fetch` (1), `test_agent_env_advertisement` (1),
`test_a2a_reason_roundtrip` (2), `test_search_giant_line_containment` (6), `test_file_write_surrogate_roundtrip` (3),
`test_terminal_compound_background` (1); `test_snapshot_session_id_leak` uses the same call and was switched too.
Production was checked for the same pattern: the bare-`bash` spawns there are POSIX-only branches
(`gateway/slash_commands.py`, `gateway/run_shutdown.py` return early on Windows), PowerShell on Windows
(`hermes_cli/dep_ensure.py`), or an argv run inside a remote sandbox (`tools/environments/remote_common.py`).

Round 1 (Antigravity): `_find_bash()` itself missed a Git for Windows install off the system drive when only
`<Git>\cmd` is on PATH (this deploy host keeps Git on `E:`); `_windows_bash_candidates` now also looks beside
the `git` on PATH. `tests/tools/test_stage2_hook_seed_one_symlinks.py` took `shutil.which("bash")` (WSL
first on Windows) and now takes `_find_bash()`.

## 2. The 42 `tests/scripts/install/` failures (39 test functions; one is parametrized 4 ways)

**Disposition: host-specific by design → `@pytest.mark.linux_only`.**

- Every one executes `scripts/install.sh` (or `setup-hermes.sh`, or a block extracted from them) under
  `bash`. Those are the Linux/macOS/Termux installers; `install.sh` refuses Windows outright
  (`CYGWIN*|MINGW*|MSYS*) … "Windows detected. Please use the PowerShell installer"`), and Windows is
  installed by `install.ps1`, whose tests in the same directory pass here.
- On this host they ran under WSL's bash (see item 1) with Windows paths, so `E:\GitHub\…\install.sh` arrived
  as `E:GitHubMagi-AGI…install.sh`; others need symlink privilege (WinError 1314), `apt`/Termux probes or
  POSIX permissions. Running them under Git Bash instead would still test a script the product never runs on
  Windows.
- `linux_only` keeps CI selection exactly as it was: unmarked tests already ran only on the Linux lane (the
  macOS/Windows lanes import only files carrying their own marker). It is selection neutrality, not new
  native macOS coverage.
- Applied per behaviour group, not per observed failure (round 1, Codex: two Termux cases still ran
  `install.sh` on Windows once Git Bash was on PATH and failed at "Windows detected"): every
  `test_install_sh_*.py` file and `test_install_macos_launcher.py` is POSIX-installer-only and carries a
  file-level `pytestmark` (merged with an existing one where present, as `test_install_sh_system_package_probe.py`
  already did). In the five files that also test `install.ps1` (autostash, commit-pin rollback, lockfile
  churn, no initial commit, unmerged index) only the `install.sh` functions are marked, so the `install.ps1`
  tests still run on Windows.

## 3. `tests/scripts/test_contributor_map.py::test_add_contributor_refuses_a_case_collision`

**Fixed in the test; production behaved correctly.** `add_contributor` returned 1 (refused the case
collision) as intended. The test then asserted `not (d / "agent@example-host.local").exists()`, which on a
case-insensitive filesystem is always true, because the existing `agent@Example-Host.local` entry matches the
lowercase name. It now asserts the directory's real entries and content (still only
`agent@Example-Host.local` → `someone`), which holds on every filesystem and still catches both a second file
(case-sensitive hosts) and an overwrite (case-insensitive hosts).

## Found alongside, NOT in A17

In the same bare-`bash` test files, 5 more failures had other host-specific causes; all are test assumptions,
none a product defect.

**Fixed (both in `tests/tools/test_docker_environment.py`; the second one also wrote mangled-name junk files
into the checkout on every Windows run, WSL treating `C:\...` as a relative file name):**

- `test_persistent_bind_mounts_survive_a_session_key_task_id`: asserted no `:` in the bind source and exactly
  one `:` in the spec, but a Windows host path keeps its drive colon (`C:\...`, which Docker Desktop parses).
  Both checks now look past `os.path.splitdrive`; a session key's colons are still caught on every host.
- `test_wrapped_exec_scopes_explicit_forward_env_across_profiles`: its fake `docker exec` ran the in-container
  command with a bare `bash` (WSL here, exit 126). It now uses `_find_bash()`.

**Left for the coordinator (they touch the conftest live-system guard, which deserves its own review):**

| Test | Cause on Windows | Suggested disposition |
|---|---|---|
| `tests/test_live_system_guard_self_test.py::test_pty_spawn_systemctl_blocked` | `pty` needs `termios` (POSIX-only module) | mark `linux_only` (pty is POSIX) |
| `tests/test_live_system_guard_self_test.py::test_os_kill_blocks_foreign_pid` | `FOREIGN_PID = 1` is init on POSIX but does not exist on Windows; the guard rightly treats a missing PID as stale and lets the call through, and `os.kill(1, …)` then raises WinError 87. **Not a guard hole**: a live foreign PID is still blocked | test: a per-host foreign PID that exists and cannot be harmed (PID 4, System, on Windows) |
| `tests/test_live_system_guard.py::test_gateway_start_inside_a_container_exec_is_not_blocked` | the guard's `_first_token_basename` splits with POSIX `shlex` and on `/` only, so `C:\...\stub-bin\docker` is not recognised as `docker` and the call is blocked (a Windows false positive of the guard, tests only); and the `#!/bin/sh` stub cannot execute on Windows anyway | guard: basename on both separators (and `.exe`) with non-POSIX `shlex` on Windows; test: a `.cmd` stub on Windows, or mark POSIX |
