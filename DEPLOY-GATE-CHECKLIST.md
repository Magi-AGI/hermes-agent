# Hermes deploy gate — everything identified must be closed before the next deploy

Lake (2026-09-24): no deploy until the whole identified hardening plan is complete; ideally all remaining issues are
found and fixed too. Past passes shipped with issues that became blockers for his multi-workflow development.
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
| A7 | Upstream `test_shell_hooks` writes mangled-name files into the working tree on Windows (recurs on every run, incl. reviewers'; must force all temp paths under the sandbox TEMP). Also an older untracked `%SystemDrive%/ProgramData/...` tree in the repo root (2026-09-06, predates the runner sandbox; an unexpanded env var): find the writer, then delete | OPEN |
| A8 | Source scans (`test_model_alias_credentials`, config-read guard) trip on nested `.claude/worktrees` | OPEN (worktree cleanup, Lake OK'd "later") |
| A9 | Flaky under load: fast-lane first-chunk timing, late-ack TTL, update-handoff-marker PowerShell | OPEN (fix or de-flake) |
| A10 | Bucket C residual R3: the first turn of a NEW session skips the durable lease (no session row yet, `agent/turn_facade_lease.py:310`). Prove production entry points serialize concurrent first turns for the same new session id, or fix it | OPEN |
| A11 | Bucket C residual R1: registry lag after a mid-turn rotation (a 2nd window can claim the new id while the durable lease still serializes its turns). Lake chose (c) accept + document on 2026-09-23 (sync map); revisit (a) async re-anchor only if seen in practice | ACCEPTED BY LAKE |
| A12 | Bucket C residual R4: orphan detection latency (parent-guard confirmations + drain up to 10 s). Transcript fencing stops an orphan's WRITES mixing with a successor's; it does NOT prove the orphan's running tool has stopped. Needs its own closure, tied to G1/D2 child lifecycle | OPEN |

## B. Phase 2b — credential recovery (Codex only)
| # | item | status |
|---|---|---|
| B1 | Design (DESIGN-CREDENTIAL-RECOVERY-PROTOCOL-2026-09-24.md) cleared by both reviewers | STAGE 1 CLEARED (rev 8: Antigravity ratified; Codex approved stage 1 with notes) |
| B1a | Rev 9 (stage-2 spec, Codex): (1) one atomic compensation decision carrying the payload ref, fixed target epochs and running status, after which recovery only does APPLY-2/COMMIT-2 and never re-allocates or re-bumps; (2) recovery's own-reservation exception: continuing/compensating a validated op id is distinct from fresh admission (applies to G4 admission and the store-lock helper) | OPEN (stage 1 proceeds meanwhile) |
| B2 | Stage 1: strict writer, witnessed reads (full witnessed-save table kept in the impl spec), metadata-only saves, chain registry, migration, refresh admission, dispatcher, replacement-login retirement + removal/suppression; stage-1 interception of everything awaiting stage 2. Missing-profile-dir `_file_lock` restriction scoped to profile stores (the Nous shared-store lock also uses `_file_lock`) | IN PROGRESS |
| B3 | Stage 2: transfers, legacy rescues, profile rename/delete/restore, remaining retirement paths | OPEN |
| B4 | Benchmark (24 proc x 4 threads, shared ledger, fault matrix) passes per stage, plus deterministic tests through the REAL dispatcher + lock helpers: competing admissions between payload and decision; recovery after the compensation decision; recovery taking locks inside its own reservation. (Benchmark is evidence only once run, not by design) | OPEN |
| B5 | Resume auth contention (memory: ~13 windows race auth.json; Copilot RAW degrade) re-measured after B2 | VERIFY |

## C. Group 2 (plan phase 4) on the synced base
| # | item | status |
|---|---|---|
| G1 | Compute-host latch reset + safe in-process fallback + stdin-EOF instrumentation (incl. A6) | OPEN |
| G2 | Saved windows persist until explicit Forget (not "mirror live windows"): ordinary closes keep entries, duplicate-session windows stay distinct, age/count never evict | LARGELY DONE via A4; VERIFY in live check |
| G3 | Config-load convoy (upstream-main lock-free reads need Lake's explicit OK: exception to stable-release policy) | OPEN / LAKE |
| G4 | Shared-gateway concurrency (DESIGN-SHARED-GATEWAY-CONCURRENCY-2026-09-24.md, option A approved with notes) | OPEN |
| G5 | Compaction summary growth + renderer V8 heap retention | OPEN |
| G6 | "Gateway down" stale-warning reattach (renderer) | OPEN |
| G7 | Dropped-submit root cause (live submit trace); `routedRuntimeId` globally null | OPEN |
| G8 | MCP startup, full original scope: discovery must not block useful service after readiness is announced. Checks: cold cache, expired token, unavailable server, concurrent-profile startup; bounded discovery; child cleanup. Any dedupe keeps auth/config distinctions (a matching URL is not identity). Includes schema-cache `ttl_ms=0` | OPEN |
| G9 | Hygiene: state.db rotating backup; Unity MCP backoff + log rotation; session-list staleness; bootstrap reset storm (single-flight, move earlier per Codex); prompt-null rebuild | OPEN |
| G10 | Remaining futures-audit timeout sites (account/billing refresh safety, status probe, Home Assistant, TTS, delegation interrupt join, context_references) | OPEN |
| G11 | Plugin autocomplete bounding — only if a plugin provider is installed | VERIFY (none installed?) |

## D. Recorded work-blocking incident fronts (in scope; each needs its own closure evidence)
| # | item | status |
|---|---|---|
| D1 | Backend-pool disown recurrence: ownership + port release of a detached `gateway run` (:8642 squatted through quit-all), not only compute-host children; cross-link G1 child lifecycle | OPEN |
| D2 | Terminal-tool 420 s hang across compute-host respawns: terminal launcher/worker tree + pipe cleanup across respawns; never root-caused | OPEN |
| D3 | Two problems: (a) a running post-tool hook wedging a worker; (b) a descendant keeping subprocess output pipes open. Orphan-future cancellation fixes neither | OPEN |
| D4 | Session-ownership lease race ("already has a live owner") — re-verify on the synced lease model | VERIFY |
| D5 | Session-message OOM (unbounded fetchall) — upstream paging `49e6d661a0` arrives with sync | VERIFY |
| D6 | Compaction slowed under concurrent load (up to ~8 min) — measure under G4/G5 | VERIFY |
| D7 | 09-07 whole-process freeze (all threads frozen). Closure = resolution or validated mitigation; a watcher or dump is diagnosis only. Capture native stacks where Python dumps are identical | OPEN |
| D8 | OS freeze via 8 GB Hermes spike (2026-08-26) — memory bound under G5 | VERIFY |
| D9 | New-window session-selection regression — fixed in A4 (82972e687a port); confirm live | VERIFY |

## E. Skills (plan phase 5)
| # | item | status |
|---|---|---|
| E1 | Pass A remainder (private-wiki-backup-environments, decko-local-browser-write-flows, AUDIT sweep) | OPEN / LAKE (permission rule for hermes-skills-audit-20260922/**) |
| E2 | Pass B: slim triad-review-workflows, persistent-agent-triad, wiki-source-triangulation; collapse reference sprawl; delete superseded | OPEN |
| E3 | `curator.consolidate` decision (after Pass B) | LAKE |
| E4 | Lake's additional skills-improvement ideas | LAKE |

## F. Deploy (last)
Timing (Codex): F1-F3 and F7-F9 are prerequisites and deploy steps; F4-F5 are deploy acceptance; F6 is post-deploy
measurement and does not gate the deploy. This changes timing only, not the rule that every identified fix lands first.

| # | item | status |
|---|---|---|
| F1 | Freeze the exact reviewed commit + matching desktop build (pinned Node v26.10.0), matching Python dependencies, STT capability, and `web_dist` | OPEN |
| F2 | Restart the messaging gateway in the same window (re-identify its process; old PID 49956 is historical) | LAKE |
| F3 | `hermes doctor` as the last deploy step (config-version migration) | OPEN |
| F7 | Verified (restore-tested) backups of state.db / compaction.db, saved windows (Roaming\Hermes), skills, and credential-protocol state | OPEN |
| F8 | Tested credential ROLLBACK policy: restoring an old auth.json after rotation can replay consumed tokens, and old code ignores the new registry's fences, so a backup alone is not a safe rollback. Define and test the rollback path | OPEN |
| F9 | Relay (`hermes dashboard`) restart by verified PID, fresh-process check (new PID StartTime), phone path verified | LAKE |
| F4 | A4 live check (criteria 3-4 + rounds 3-6 additions); per-renderer memory telemetry survives reboot (schtasks) | LAKE |
| F5 | Active acceptance on the real ~27-window workload: concurrent prompts, tools, approvals, stop, reconnect, compaction, rewind; persisted results + resource recovery checked; the shared-gateway design's acceptance notes. Idle health probes are not enough | OPEN |
| F6 | Post-deploy: Lake resumes workstreams; measure skill_view/day + views/write vs 200.7 / 2.21 | POST-DEPLOY |
