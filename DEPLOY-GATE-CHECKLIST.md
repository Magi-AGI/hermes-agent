# Hermes deploy gate — everything identified must be closed before the next deploy

Lake (2026-09-24): no deploy until the whole identified hardening plan is complete; ideally all remaining issues are
found and fixed too. Past passes shipped with issues that became blockers for his multi-workflow development.
Parallel tracks T1-T6 (PARALLEL-TRACK-PROMPTS-2026-09-24.md) own sections A/C/D/E items as listed there; this session owns B and integration.
Every line below must be CLOSED (fixed + reviewed + tested) or explicitly re-scoped BY LAKE. Reviewers: please audit
this list for anything missing, not only the status of what's here.

Audits: Antigravity 2026-09-24 (complete; notes applied to B1/A7/D1). Codex 2026-09-24 (complete; its 6 should-fixes
applied: residuals A10-A12, D scope + closure evidence, G8 full MCP scope, G2/B3 wording, F deploy/rollback gate,
closure-evidence rule).

Closure rule (Codex): baseline parity shows NO REGRESSION; it does not close a known defect. Every remaining test
failure and every excluded item carries a written disposition. An incident front (D) closes on a fix or a validated
mitigation, not on a watcher or a dump.

Status key: DONE (on sync branch, triad-approved) / OPEN / IN PROGRESS / VERIFY (needs live evidence) / LAKE (needs his action) / ACCEPTED BY LAKE.

## A. Upstream sync (magi/sync-v2026.9.21)
| # | item | status |
|---|---|---|
| A1 | Buckets 0, A1, A2, A3, A4, B, C, D + review follow-ups | DONE |
| A2 | Final validation against an IMMUTABLE candidate commit through the isolated runner: full A/B, a written disposition for every remaining failure, process-leak check (no stray children, ports, or lock files after the run) | OPEN (at the very end) |
| A3 | A4 residual R-A4-1: reload can let Reopen advance before the replacement document commits | OPEN |
| A4 | A4 residual R-A4-2: null report token passes when no document is current | OPEN |
| A5 | Yuanbao recall redaction rewrites the whole transcript unfenced (targeted, fenced redaction) | OPEN |
| A6 | Compute-host fallback session: a full turn through it stalls after message.start | OPEN (under G1) |
| A7 | Upstream `test_shell_hooks` writes mangled-name files into the working tree on Windows (recurs on every run, incl. reviewers'; must force all temp paths under the sandbox TEMP). Also an older untracked `%SystemDrive%/ProgramData/...` tree in the repo root (2026-09-06, predates the runner sandbox; an unexpanded env var): find the writer, then delete | DONE (T5, Codex + Antigravity r4; on sync branch; runner now fails a green run if the checkout gains files; junk trees deleted 2026-09-26) |
| A8 | Source scans (`test_model_alias_credentials`, config-read guard) trip on nested `.claude/worktrees` | DONE (T5; scans skip nested worktrees/other checkouts via one shared rule) |
| A9 | Flaky under load: fast-lane first-chunk timing, late-ack TTL (`test_late_ack_handlers_are_bounded_by_ttl_and_cap`), update-handoff-marker PowerShell, `test_peer_dm_into_open_bot_chat` (streamed), `test_profiles_sidebar_cache.py::test_projects_tree_coalesces_concurrent_scans_and_returns_copies` (1 s wait; flaked under load 2/2 full runs) | DONE (T5: all named flakes incl. sidebar-cache, Codex r3; on sync branch) |
| A16 | Unowned older flakes (09-06 baseline): test_hosted_room_prompt_fence, test_compute_host_turn_protocol, test_slash_worker_mcp_discovery, test_transcription_tools, test_auxiliary_explicit_cancellation, test_coding_context; plus ~10 desktop electron tests that spawn real processes under vitest's 5 s default (set one testTimeout for the electron project) | DONE (T5, Codex r5; six older flakes + electron test timeout; on sync branch through 0f6fb86eba; combined run 137 passed) |
| A17 | A2 dispositions recorded by T5 (same failure on base): test_file_ops_cwd_tracking::test_patch_returns_success_only_when_file_actually_written; 42 tests/scripts/install/ (Linux install script under Git Bash); 1 test_contributor_map (case-insensitive FS); 38 desktop electron tests failing on every Windows run (list in T5 a16\RECEIPTS.md). gateway-2 session assigned A17 | DISPOSITION NEEDED at A2 |
| A18 | From T1: six more files use the same 10 s lock pattern (curator, trajectory, vault_store, plugins_state, install_identity, honcho/oauth); `_skill_mutation_locks` doesn't clean up when a later lock fails (pre-existing); upstream `persistence_live.py` leaves real USERPROFILE in place so its test backend can reach the real host-lock folder | DONE (gateway-2, Codex + Antigravity r2; on sync branch through 54d720213d; +cron/jobs.py lock site. Caveats: site tests red only on Windows; pre-existing lock-file padding race when two processes create one lock at once; eval scripts' env sealing doesn't refuse a checkout .env) |
| A19 | Product follow-ups from T5: under load the start-of-session workspace summary can drop lines when its 2.5 s git probe times out; on a loaded machine the compute host can send turn.end >3 s after message.complete | OPEN (unowned) |
| A20 | Upstream behaviour (from stage-1 r13 comparison): restoring over a CORRUPT sessions/state db (empty main file, damaged schema; page-size case on POSIX) discards the old WAL via upstream's fallback / SQLite close, identically on baseline. Not credential-affecting. Needs a disposition (accept as upstream, or a small upstream-aligned fix) | DONE pending B2 close (Codex verified the WAL fix at db94c4b5b9) |
| A21 | Upstream: `Connection.backup` page copy retries forever while the destination is write-locked by another process, so a restore hangs indefinitely (found in stage-1 r14). A hang = blocker class for Lake; needs a bounded wait or refusal | IN PROGRESS (restore paths bounded; timeout accounting fix + doctor_state.py / kanban_transfer.py callers in stage-1 round 16; Codex + Antigravity: both callers in scope) |
| A22 | New flake: test_minimax_oauth.py::test_refresh_error_body_bounded_and_readable_with_real_client (local-socket WinError 10054 once, passed on retry, stage-1 r15 auth run) | OPEN (unowned) |
| A10 | Bucket C residual R3: the first turn of a NEW session skips the durable lease (no session row yet, `agent/turn_facade_lease.py:310`). Prove production entry points serialize concurrent first turns for the same new session id, or fix it | DONE (Codex + Antigravity; on sync branch a9378a1ee3..8607f59f02). Lake chose A: keep upstream derived-id grouping |
| A13 | TUI `prompt.submit` checks `running` and claims it under two separate lock acquisitions (`tui_gateway/methods_prompt.py:637-639` vs `:551`): two submits in one process can both pass; only the durable lease backstops it (found by A10 audit) | DONE (5b0e027a7b; Codex + Antigravity) |
| A14 | Lease admission: a FAILED row probe is treated as "row exists" and sets `_session_db_created=True`, which suppresses lazy row creation for a genuinely new session (pre-existing; found by A10 audit) | DONE (92b23cfb66; Codex + Antigravity) |
| A15 | `tests/tui_gateway/test_compute_host_phase1.py` hangs on both base and branch (excluded from A10 batches); needs a disposition or fix before A2 | OPEN |
| A11 | Bucket C residual R1: registry lag after a mid-turn rotation (a 2nd window can claim the new id while the durable lease still serializes its turns). Lake chose (c) accept + document on 2026-09-23 (sync map); revisit (a) async re-anchor only if seen in practice | ACCEPTED BY LAKE |
| A12 | Bucket C residual R4: orphan detection latency (parent-guard confirmations + drain up to 10 s). Transcript fencing stops an orphan's WRITES mixing with a successor's; it does NOT prove the orphan's running tool has stopped. Needs its own closure, tied to G1/D2 child lifecycle | OPEN |

## B. Phase 2b — credential recovery (Codex only)
| # | item | status |
|---|---|---|
| B1 | Design (DESIGN-CREDENTIAL-RECOVERY-PROTOCOL-2026-09-24.md) cleared by both reviewers | DONE (rev 9: Codex approved with notes, cleared for stage 2; Antigravity ratified). One Codex test-matrix nit (split pre/post-DECISION-2 abandonment outcomes + negative continuation tests) folded into the stage-2 test plan |
| B1a | Rev 9 (stage-2 spec, Codex): (1) one atomic compensation decision carrying the payload ref, fixed target epochs and running status, after which recovery only does APPLY-2/COMMIT-2 and never re-allocates or re-bumps; (2) recovery's own-reservation exception: continuing/compensating a validated op id is distinct from fresh admission (applies to G4 admission and the store-lock helper) | DONE (rev 9, both reviewers) |
| B2 | Stage 1: strict writer, witnessed reads (full witnessed-save table kept in the impl spec), metadata-only saves, chain registry, migration, refresh admission, dispatcher, replacement-login retirement + removal/suppression; stage-1 interception of everything awaiting stage 2. Missing-profile-dir `_file_lock` restriction scoped to profile stores (the Nous shared-store lock also uses `_file_lock`) | IN PROGRESS round 16 (at db94c4b5b9: Antigravity approved; Codex: WAL blocker FIXED, no blockers; should-fix: lock budget measured 17.4 s vs 10 s (implicit SQLite wait before the timer) -> disable implicit wait, start clock before copy) |
| B3 | Stage 2: transfers, legacy rescues (incl. rescue files beside a symlinked alias path), profile rename/delete/restore, remaining retirement paths (symlinked-alias rescue DISCOVERY moved to stage 1; archival stays here), and a fence-preserving RESTORE of Codex credential state (stage 1 strips it; stage 2 design must meet Codex round-7 requirements: local manifest never overwritten by archived markers, protocol-destination recovery, strict marker schema, unreadable barrier blocks, audited clear-barrier command) | OPEN |
| B4 | Benchmark (24 proc x 4 threads, shared ledger, fault matrix) passes per stage, plus deterministic tests through the REAL dispatcher + lock helpers: competing admissions between payload and decision; recovery after the compensation decision; recovery taking locks inside its own reservation. (Benchmark is evidence only once run, not by design) | OPEN |
| B5 | Resume auth contention (memory: ~13 windows race auth.json; Copilot RAW degrade) re-measured after B2 | VERIFY |

## C. Group 2 (plan phase 4) on the synced base
| # | item | status |
|---|---|---|
| G1 | Compute-host latch reset + safe in-process fallback + stdin-EOF instrumentation (incl. A6) | OPEN |
| G2 | Saved windows persist until explicit Forget (not "mirror live windows"): ordinary closes keep entries, duplicate-session windows stay distinct, age/count never evict | LARGELY DONE via A4; VERIFY in live check |
| G3 | Config-load convoy (upstream-main lock-free reads need Lake's explicit OK: exception to stable-release policy) | DONE (T1: 4 upstream/main config.py lock-free read commits cherry-picked, config.py only) |
| G4 | Shared-gateway concurrency (DESIGN-SHARED-GATEWAY-CONCURRENCY-2026-09-24.md, option A approved with notes) | DONE (T1 magi/track-gateway, Codex + Antigravity; on sync branch through 6951c62e10; 27-window harness 20/20 grades) |
| G5 | Compaction summary growth + renderer V8 heap retention | OPEN |
| G6 | "Gateway down" stale-warning reattach (renderer) | OPEN |
| G7 | Dropped-submit root cause (live submit trace); `routedRuntimeId` globally null | OPEN |
| G8 | MCP startup, full original scope: discovery must not block useful service after readiness is announced. Checks: cold cache, expired token, unavailable server, concurrent-profile startup; bounded discovery; child cleanup. Any dedupe keeps auth/config distinctions (a matching URL is not identity). Includes schema-cache `ttl_ms=0` | OPEN |
| G9 | Hygiene: state.db rotating backup; Unity MCP backoff + log rotation; session-list staleness; bootstrap reset storm (DONE by T1); prompt-null rebuild | OPEN |
| G10 | Remaining futures-audit timeout sites (account/billing refresh safety, status probe, Home Assistant, TTS, delegation interrupt join, context_references) | OPEN |
| G11 | Plugin autocomplete bounding — only if a plugin provider is installed | VERIFY (none installed?) |

## D. Recorded work-blocking incident fronts (in scope; each needs its own closure evidence)
| # | item | status |
|---|---|---|
| D1 | Backend-pool disown recurrence: ownership + port release of a detached `gateway run` (:8642 squatted through quit-all), not only compute-host children; cross-link G1 child lifecycle | OPEN |
| D2 | Terminal-tool 420 s hang across compute-host respawns: terminal launcher/worker tree + pipe cleanup across respawns; never root-caused | OPEN |
| D3 | Two problems: (a) a running post-tool hook wedging a worker; (b) a descendant keeping subprocess output pipes open. Orphan-future cancellation fixes neither | OPEN |
| D4 | Session-ownership lease race ("already has a live owner") — re-verify on the synced lease model | DONE (T1 re-verified on the synced lease model) |
| D5 | Session-message OOM (unbounded fetchall) — upstream paging `49e6d661a0` arrives with sync | VERIFY |
| D6 | Compaction slowed under concurrent load (up to ~8 min) — measure under G4/G5 | MEASURED (T1 harness); confirm on live workload in F5 |
| D7 | 09-07 whole-process freeze (all threads frozen). Closure = resolution or validated mitigation; a watcher or dump is diagnosis only. Capture native stacks where Python dumps are identical | DONE (T1: freeze supervisor detect/dump/kill tested against a real frozen backend; desktop auto-restart after kill = existing crash-recovery path, confirm in F5) |
| D8 | OS freeze via 8 GB Hermes spike (2026-08-26) — memory bound under G5 | VERIFY |
| D9 | New-window session-selection regression — fixed in A4 (82972e687a port); confirm live | VERIFY |

## E. Skills (plan phase 5)
| # | item | status |
|---|---|---|
| E1 | Pass A remainder (private-wiki-backup-environments, decko-local-browser-write-flows, AUDIT sweep) | DONE (T6, both reviewers through f83e848, incl. Pass C depersonalization); live apply = F10 |
| E2 | Pass B: slim triad-review-workflows, persistent-agent-triad, wiki-source-triangulation; collapse reference sprawl; delete superseded | DONE (T6, both reviewers through f83e848); live apply = F10 |
| E5 | Skill-learning bug (T6 finding): Hermes's learner treats reviewer text Lake pastes as Lake's own corrections. Hermes code fix; confirming from real transcripts needs Lake's OK to read a COPY of claudetriad/state.db | OPEN - owner needed (Hermes learner code: injected summaries / consent blocks / pasted reviewer text treated as Lake's words). Recommended owner: T6 |
| E6 | Team skills export without personal skills (job-application-workflows, household-finance reference, business-entity-formation-planning); minor upstream skill issues (format_findings broken with no callers; bundled grounded-citations references archived research-paper-writing) | DONE (T6: team zip export/team-skills-5eb0696.zip, host IPs placeholdered, both reviewers); minor upstream skill issues noted, no deploy impact |
| E3 | `curator.consolidate` decision (after Pass B) | LAKE (after E5: start with `hermes curator run --dry-run --consolidate`) |
| E4 | Lake's additional skills-improvement ideas | LAKE |

## F. Deploy (last)
Timing (Codex): F1-F3 and F7-F9 are prerequisites and deploy steps; F4-F5 are deploy acceptance; F6 is post-deploy
measurement and does not gate the deploy. This changes timing only, not the rule that every identified fix lands first.

| # | item | status |
|---|---|---|
| F1 | Freeze the exact reviewed commit + matching desktop build (pinned Node v26.10.0), matching Python dependencies, STT capability, and `web_dist` | OPEN |
| F2 | Restart the messaging gateway in the same window (re-identify its process; old PID 49956 is historical) | LAKE |
| F3 | `hermes doctor` as the last deploy step (config-version migration) | OPEN |
| F10 | T6 tooling + F10-PROCEDURE.md approved through a3538b6 (both reviewers); T6 runs the drift merge the day before deploy (4 known live drifts, genuine lessons). Skills live apply (T6 audit repo): with all sessions, gateway and desktop backend stopped: `apply_to_live.py --apply` (plans 78 modified / 106 added / 527 removed, backs up first, refuses on drift), `--verify`, then the six printed `hermes -p claudetriad curator archive` commands (reversible via curator restore). Do it in the deploy window | LAKE |
| F7 | Verified (restore-tested) backups of state.db / compaction.db, saved windows (Roaming\Hermes), skills, and credential-protocol state | OPEN |
| F8 | Tested credential ROLLBACK policy: restoring an old auth.json after rotation can replay consumed tokens, and old code ignores the new registry's fences, so a backup alone is not a safe rollback. Define and test the rollback path | OPEN |
| F9 | Relay (`hermes dashboard`) restart by verified PID, fresh-process check (new PID StartTime), phone path verified | LAKE |
| F4 | A4 live check (criteria 3-4 + rounds 3-6 additions); per-renderer memory telemetry survives reboot (schtasks) | LAKE |
| F5 | (T1 carry-forward: model-picker latency 1.6-3.72 s vs a 3 s limit, watch it; desktop auto-restart after a D7 freeze-kill not exercised by the harness) Active acceptance on the real ~27-window workload: concurrent prompts, tools, approvals, stop, reconnect, compaction, rewind; persisted results + resource recovery checked; the shared-gateway design's acceptance notes. Idle health probes are not enough | OPEN |
| F6 | Post-deploy: Lake resumes workstreams; measure skill_view/day + views/write vs 200.7 / 2.21 | POST-DEPLOY |
