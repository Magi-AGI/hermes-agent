# Hermes deploy gate — everything identified must be closed before the next deploy

Lake (2026-09-24): no deploy until the whole identified hardening plan is complete; ideally all remaining issues are
found and fixed too. Past passes shipped with issues that became blockers for his multi-workflow development.
Every line below must be CLOSED (fixed + reviewed + tested) or explicitly re-scoped BY LAKE. Reviewers: please audit
this list for anything missing, not only the status of what's here.

Audits: Antigravity 2026-09-24 (complete; notes applied to B1/A7/D1). Codex: pending.

Status key: DONE (on sync branch, triad-approved) / OPEN / IN PROGRESS / VERIFY (needs live evidence) / LAKE (needs his action).

## A. Upstream sync (magi/sync-v2026.9.21)
| # | item | status |
|---|---|---|
| A1 | Buckets 0, A1, A2, A3, A4, B, C, D + review follow-ups | DONE |
| A2 | Full combined A/B at final HEAD (only baseline failures remain) | OPEN (rerun at the very end) |
| A3 | A4 residual R-A4-1: reload can let Reopen advance before the replacement document commits | OPEN |
| A4 | A4 residual R-A4-2: null report token passes when no document is current | OPEN |
| A5 | Yuanbao recall redaction rewrites the whole transcript unfenced (targeted, fenced redaction) | OPEN |
| A6 | Compute-host fallback session: a full turn through it stalls after message.start | OPEN (under G1) |
| A7 | Upstream `test_shell_hooks` writes mangled-name files into the working tree on Windows (recurs on every run, incl. reviewers'; must force all temp paths under the sandbox TEMP) | OPEN |
| A8 | Source scans (`test_model_alias_credentials`, config-read guard) trip on nested `.claude/worktrees` | OPEN (worktree cleanup, Lake OK'd "later") |
| A9 | Flaky under load: fast-lane first-chunk timing, late-ack TTL, update-handoff-marker PowerShell | OPEN (fix or de-flake) |

## B. Phase 2b — credential recovery (Codex only)
| # | item | status |
|---|---|---|
| B1 | Design (DESIGN-CREDENTIAL-RECOVERY-PROTOCOL-2026-09-24.md) cleared by both reviewers | IN PROGRESS (rev 7: Antigravity ratified, Codex 1 blocker + 2 should-fix on stage-2 lifecycle; rev 8 drafting) |
| B2 | Stage 1: strict writer, witnessed reads, metadata-only saves, chain registry, migration, refresh admission, dispatcher; stage-1 interception of everything awaiting stage 2 | OPEN |
| B3 | Stage 2: transfers, legacy rescues, retirement, profile rename/delete/restore | OPEN |
| B4 | Benchmark (24 proc x 4 threads, shared ledger, fault matrix) passes per stage | OPEN |
| B5 | Resume auth contention (memory: ~13 windows race auth.json; Copilot RAW degrade) re-measured after B2 | VERIFY |

## C. Group 2 (plan phase 4) on the synced base
| # | item | status |
|---|---|---|
| G1 | Compute-host latch reset + safe in-process fallback + stdin-EOF instrumentation (incl. A6) | OPEN |
| G2 | Window store mirrors live windows ("restored N of M") | LARGELY DONE via A4; VERIFY in live check |
| G3 | Config-load convoy (upstream-main lock-free reads need Lake's explicit OK: exception to stable-release policy) | OPEN / LAKE |
| G4 | Shared-gateway concurrency (DESIGN-SHARED-GATEWAY-CONCURRENCY-2026-09-24.md, option A approved with notes) | OPEN |
| G5 | Compaction summary growth + renderer V8 heap retention | OPEN |
| G6 | "Gateway down" stale-warning reattach (renderer) | OPEN |
| G7 | Dropped-submit root cause (live submit trace); `routedRuntimeId` globally null | OPEN |
| G8 | MCP dedupe + progress line, only if the synced base still shows a problem | VERIFY |
| G9 | Hygiene: state.db rotating backup; Unity MCP backoff + log rotation; session-list staleness; bootstrap reset storm (single-flight, move earlier per Codex); prompt-null rebuild | OPEN |
| G10 | Remaining futures-audit timeout sites (account/billing refresh safety, status probe, Home Assistant, TTS, delegation interrupt join, context_references) | OPEN |
| G11 | Plugin autocomplete bounding — only if a plugin provider is installed | VERIFY (none installed?) |

## D. Memory-noted fronts not explicitly in the plan (audit each)
| # | item | status |
|---|---|---|
| D1 | Backend-pool disown recurrence: orphaned `gateway run` squatting :8642 through quit-all (hermes-desktop-backend-pool-bug) | OPEN — not in plan; cross-link G1: child-process lifecycle (kill-on-close job object or explicit PID tracking) |
| D2 | Terminal-tool 420s hang across compute-host respawns (hermes-terminal-tool-hang), never root-caused | OPEN — not in plan |
| D3 | Post-tool-hook wedge + subprocess pipe leak | OPEN — check vs G10 |
| D4 | Session-ownership lease race ("already has a live owner") — re-verify on the synced lease model | VERIFY |
| D5 | Session-message OOM (unbounded fetchall) — upstream paging `49e6d661a0` arrives with sync | VERIFY |
| D6 | Compaction slowed under concurrent load (up to ~8 min) — measure under G4/G5 | VERIFY |
| D7 | 09-07 whole-process freeze (all threads frozen) — capture evidence (py-spy on stall > 5 s) before code | OPEN |
| D8 | OS freeze via 8 GB Hermes spike (2026-08-26) — memory bound under G5 | VERIFY |
| D9 | New-window session-selection regression — fixed in A4 (82972e687a port); confirm live | VERIFY |

## E. Skills (plan phase 5)
| # | item | status |
|---|---|---|
| E1 | Pass A remainder (private-wiki-backup-environments, decko-local-browser-write-flows, AUDIT sweep) | OPEN / LAKE (permission rule for hermes-skills-audit-20260922/**) |
| E2 | Pass B: slim triad-review-workflows, persistent-agent-triad, wiki-source-triangulation; collapse reference sprawl; delete superseded | OPEN |
| E3 | `curator.consolidate` decision (after Pass B) | LAKE |
| E4 | Lake's additional skills-improvement ideas | LAKE |

## F. Deploy itself (last)
| # | item | status |
|---|---|---|
| F1 | Freeze the exact reviewed commit + matching desktop build (pinned Node v26.10.0) | OPEN |
| F2 | Restart the messaging gateway in the same window (re-identify its process; old PID 49956 is historical) | LAKE |
| F3 | `hermes doctor` as the last deploy step (config-version migration) | OPEN |
| F4 | A4 live check (criteria 3-4 + rounds 3-6 additions) | LAKE |
| F5 | Per-renderer memory telemetry survives reboot (schtasks) | LAKE |
| F6 | Active soak, then Lake resumes workstreams; measure skill_view/day + views/write vs 200.7 / 2.21 | OPEN |
