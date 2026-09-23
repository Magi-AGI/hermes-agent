# Bucket C (session ownership) — supersession analysis vs v2026.9.21

Scope: sync bucket C = the 19 fork commits `aa06b3969b^..6afa0cecac` + `5a41e8aa96` (phase-0
telemetry). Analysis only, no code changed. Sync branch reference: `magi/sync-v2026.9.21` @ `5548352fea`.
Upstream sources were read at tag `v2026.9.21`. Line numbers below are at the tag.

## Headline

The fork and upstream fixed the **same root cause**, the §3a double-claim: the compute-host child
re-claims the parent's lease under its own pid and gets "already has a live owner". They did it
with **two incompatible ownership models**:

| | Fork (bucket C, live since 09-11) | Upstream v2026.9.21 |
|---|---|---|
| Child's lease | real delegated registry claim (owner_pid, executor pid/start-time binding, generations) | inert borrowed token `enabled=False`, `released=False` (`session_lifecycle.py:88-118`, b397349065 + ecefbfa6e1) |
| Who writes the registry | parent and child (delegated, fenced by executor binding) | parent only |
| Compression A→B inside child | child transfers the delegated entry in place; if the transfer fails, the session is quarantined | child only retargets its token locally; parent re-anchors its real lease when it adopts `session_key` from `turn.end` / `control.ack` (`compute_host_bridge.py:101-116`, ecefbfa6e1) |
| Close during isolated turn | cleanup-owner gate, admission fence, drain-before-finalize (round-14 passes 5-9) | close interrupts and waits a grace period; if the turn hasn't settled, the real lease is deferred and released on turn.end/turn.error (`session_lifecycle.py:530-563`, baeebe4a60) |
| Parent death mid-turn | executor-liveness binding keeps the entry alive while the child executes (07a5b51a4d + rounds) | **not covered** |

Porting the fork commits on top would run both models at once. **Recommendation: do not port the
19 commits.** Take upstream's model, then close the specific gaps it still has (G1-G3 below) with
small, targeted fixes on upstream's shape. The fork's integration tests are worth salvaging as red
tests for those gaps; its code is not.

## Per-commit table

Conflict surface comes from a trial `git merge-tree` of the whole range onto `5548352fea`. The
range conflicts in 6 files: `active_sessions.py`, `test_active_sessions.py`, `compute_host.py`,
`compute_host_bridge.py`, `methods_slash.py`, `session_lifecycle.py`. `compute_host.py` is the
worst: the fork adds +1077 lines to a file that upstream has restructured to 641 lines.

| commit | fixes | upstream status | rec | conflict |
|---|---|---|---|---|
| aa06b3969b Stage 1: registry delegation/transfer/release rules | §3a child re-claim; transfer and release under delegation | **Fixed, different design**: b397349065 (borrow), ecefbfa6e1 (borrow stays inert across rotation; parent re-anchors). Every release entrypoint is safe because the borrowed `lease_id` is `borrowed:*` and never matches a registry entry (`active_sessions.py:577-593`), and transfer on `enabled=False` is local-only (`:603-605`) | drop | active_sessions.py, session_lifecycle.py, compute_host.py |
| 07a5b51a4d Stage 2: executor-liveness bind/detach/verify | parent-death exposure (Codex's round-7 production gate) | **Not covered** (see G3) | drop; re-derive only if Lake picks G3 option (c) | active_sessions.py (+310) vs upstream liveness rework c53d5b741c, b96e60b2a8, 052e3f7147, c2cb1df335 |
| f68e11197b Stage 2/3: host admission, ref-counted detach, parent release signal, integration tests | wiring for the delegated model | Model superseded. Close-during-turn is covered by baeebe4a60 | drop; salvage test scenarios (see G1-G3) | compute_host.py, bridge, host_supervisor, methods_session, methods_slash |
| e2e1950ea0 delegation detection by owner_pid; transfer/compression fail closed without a lease | delegation false-positives; unfenced continuation | Owner_pid detection is moot without delegation. Upstream's vouch is `{lease_id, session_id}` and is installed only when the vouch names the admitted stored id (`session_lifecycle.py:108-113`) | drop | session_lifecycle.py, session_compression.py |
| 9d13c3b2ff round 11: poller quiescence, compression agent reuse, auto-cleanup notify, generation races | fork machinery races | Fork-only machinery | drop | compute_host.py, session_compression.py |
| 40867caba9 round 13: poller join, non-persisting compression quarantine, cold-construction gen race | quarantine when a delegated transfer fails | Moot: the child's transfer can't fail under upstream's model. The residual window is G2 | drop | compute_host.py, session_compression.py, session_notifications.py |
| de7d254499 round 14: fail-closed poller join, dual quarantine flags, host-coordinated release | same | Fork-only | drop | compute_host.py, prompt_turn.py, session_compression.py, session_lifecycle.py |
| a9e0e27660 R14 post 1: active-host module identity, shutdown persist, stale-gen recovery | child shutdown persistence | Partly: upstream `ComputeHost.shutdown` drains live turns, then flushes (skipping live sids) (`compute_host.py:100-121`) | drop | compute_host.py |
| eaf99848f4 R14 post 2: quarantine at the shared choke point, shutdown quiescence | same | Fork-only / partly as above | drop | compute_host.py, session_lifecycle.py, session_reaper.py |
| 17d5a5063d R14 post 3: self-join in drain, bounded turn-chain drain | drain correctness | Upstream drain is deadline-bounded (`compute_host.py:109-117`) | drop | compute_host.py |
| 018c7f41cf R14 post 4: cross-thread release/quarantine deadlock | fork-introduced deadlock | Fork-only | drop | compute_host.py |
| ee95dfac54 R14 post 5: single cleanup owner per sid | racing finalizers | Upstream `_finalize_session` is a one-shot latch (`session_lifecycle.py:356-358`). Close vs isolated turn is handled by baeebe4a60 | drop | compute_host.py |
| 550ca12318 R14 post 6: shutdown through the cleanup-ownership gate | same | same | drop | compute_host.py |
| 9ed40e8b6d R14 post 7: drain before finalizing | persist-before-quiescence | Upstream drains before flush (`compute_host.py:100-121`) | drop | compute_host.py |
| ea9ca0e6fb R14 post 8: fence admission for the whole cleanup | admission during cleanup | Upstream: `_session_turn_admission` / `backend_retirement` (`session_lifecycle.py:16-46`), 98745df3b9 | drop | compute_host.py, prompt_turn.py |
| 6afaaa9a18 R14 post 9: atomic fence establishment | same | same | drop | compute_host.py |
| d83c13fe7f R14 post 10: retry a stranded release after a refused generation update | executor-binding generations | Moot without executor bindings | drop | compute_host.py |
| 928c5b6c5f R14 post 11: retry tied to completion | same | Moot | drop | compute_host.py |
| 6afa0cecac R14 post 12: unified durable retry watcher | same | Moot | drop | compute_host.py |
| 5a41e8aa96 phase-0 telemetry (claimant/incumbent, resume variant, renderer `[phase0]`) | diagnosis of §3a (**done**: confirmed 09-10) | Upstream has no claimant/incumbent logging (`git grep incumbent` finds nothing). **The child never calls `setup_logging()`**, so compute-host child logs don't reach agent.log | **port slimmed**: keep the child `setup_logging()` and the refuse-path claimant/incumbent log line; drop the resume-variant and renderer `[phase0]` logs (check overlap with bucket D's submit trace 57f8c95f3b first) | none (trial merge is clean) |

## Gaps upstream still has (candidate targeted fixes)

| id | gap | evidence at tag | proposed fix on upstream's shape | size |
|---|---|---|---|---|
| **G1** | **The child's shutdown ends the parent's live durable row.** `flush_all_sessions` calls `_finalize_session(end_reason="compute_host_{shutdown,orphan,sigterm,stdin_closed}")` for every session it holds (`compute_host.py:123-135`). Those reasons are not in `_AUTOMATIC_SESSION_END_REASONS` (`session_lifecycle.py:60`), so `_tui_owns_lifecycle` stays True and the child calls `db.end_session(...)` (`:415-423`). `_end_session_on_close` also stays True, so `agent.close()` ends the row a second time. The borrowed lease doesn't change this. It contradicts #105588 (the durable row stays open on automatic cleanup) and was the fork's round-7 "second gap". | code read; not yet reproduced | In `_finalize_session`, treat a session holding a borrowed token (`enabled=False`, `lease_id` starting with `borrowed:`) as "another runtime owns the lifecycle": skip `db.end_session` and set `agent._end_session_on_close = False`. Write the test red on the tag first (salvage from the fork's "child restart does not end the parent row" scenario). | small |
| **G2** | **Mid-turn compression leaves the continuation unfenced until the turn ends.** The parent re-anchors only when it adopts `session_key` from `turn.end` / `control.ack` (`compute_host_bridge.py:101-116`, `_history_meta` at `compute_host.py:479-484`). During a long turn after auto-compression, B has no registry entry, so another runtime can claim B while the child writes to it. This is Codex's round-6 P1 class. A failed re-anchor only logs (`compute_host_bridge.py:109-111`), so the continuation stays unfenced and the parent's lease stays on A. | code read | The child emits a rotation frame at the rotation point (`session_compression.py:281`), and the parent re-anchors immediately under `history_lock` on the drain thread. Alternatively accept the window: B's id is only discoverable via state.db/`/resume`. Lake's call. | small-medium |
| **G3** | **Parent death during an isolated turn.** The registry entry's owner is the parent pid, so once the parent dies it is prunable. Meanwhile the child keeps writing through parent-guard detection (A1 now needs 3 confirmations) plus the orphan `shutdown` drain of up to 10s (`compute_host.py:100, 475`). Codex made executor liveness the production gate in round 7; Antigravity called the window acceptable. | code read | (a) Accept and document. (b) Cheap shrink: on orphan detection, interrupt live turns instead of draining writes. (c) Full executor liveness on upstream's registry: re-derive from 07a5b51a4d; it took 14 + 12 review rounds last time. Per the opportunity-cost memory, (a) or (b) is the default recommendation. | (a) 0 / (b) small / (c) large |

## Codex's host-side stale-snapshot race (#2 from the A1 review)

**It no longer exists in the sync branch, and no bucket owns it.** The race came from RelayBuffer:
it moved `turn.end` / `turn.error` off the stdout reader while control acks still applied inline, so
an older completion could land after a newer compression. With RelayBuffer dropped (A1 re-port
`8fa5ab0dbf`), every host frame is applied in emission order on the single drain thread. That
covers turn callbacks, late control handlers and `_adopt_late_compute_host_compress_ack`. The two
premises of the race also fail on upstream's shape:

- **"A restarted host starts at history_version 0."** CORRECTED (Codex review of this doc): the
  premise holds. `_init_session` (`server.py:2479`) sets `history_version` to 0 on the normal path;
  only the exception fallback (`compute_host.py:384`) copies the frame's value. The parent adopts
  `max(parent, frame)` (`compute_host_bridge.py:113-116`), so the parent's version never goes
  backwards and no stale frame can roll it back. The real consequence is the opposite one, see G4:
  after a restart, the child's bumps stay below the parent's value and are ignored until they catch
  up. Removing RelayBuffer removes its particular reordering mechanism, which is what made the
  equal-version stale frame dangerous.
- **"Equal-version stale frames after compression bumps the version before rotating the key."** Both
  happen in the child (`session_compression.py:259` and `:281`) before it emits `_history_meta`, so
  a single ordered stream cannot deliver the pre-rotation frame after the post-rotation one.

After a host dies, `_fail_pending_turns` pops its pending turns and late handlers, so any frames the
dead host left behind no longer have a callback or handler to adopt them.

What remains, and belongs to the **A1 triad**, not C: one invariant test that frames from the
previous host after a watchdog kill and respawn are never adopted. The concern is that the old
process's drain thread could overlap the new one's.

Also relevant: once a session has run an isolated turn, `_compute_host_active` keeps routing it to
the child (`compute_host_bridge.py:29-34`). That stickiness prevents a split-brain where the parent
compresses inline while a late child compress-ack is still outstanding.

## Upstream fixes that arrive with the tag and touch known fronts

| upstream | effect |
|---|---|
| 3ca367ad80, 8039bb1e61, 251e5f7c0a | a chat restored under a new runtime takes over its own detached lease, within the profile. Likely addresses the memory note "stale leases only reap on a FULL desktop restart" |
| 77915e344c, f2c3455e67, c22588bc79, 8109c400a2 | isolated turns survive disconnect; orphan interrupt and reap hygiene |
| d3a753df30, a8b8556108, dfc40a50a9 | the durable row survives automatic Desktop cleanup and `agent.close()` (parent side only, hence G1) |
| c53d5b741c, b96e60b2a8, 052e3f7147, c2cb1df335 | registry liveness robustness, including a Windows fix for the POSIX zombie probe |

## Proposed port order

1. Record the decision in the sync map: C = drop the 19 commits, take upstream's model. Nothing needs
   reverting on the sync branch, because it was cut from the tag.
2. Port the slimmed `5a41e8aa96` (child `setup_logging` + the refuse-path claimant/incumbent log).
   It merges cleanly.
3. G1: red test on the tag, fix, A/B through the runner.
4. G2 and G3, per Lake's decision.
5. Deploy note: the live registry holds entries in the fork's schema (executor fields). Swap only
   when both profiles' `active_sessions.json` are empty and backed up, as in the 09-11 runbook, and
   confirm the upstream reader ignores the extra fields.

## Open questions for Lake

1. Do you accept dropping the fork's ownership model (4,000+ reviewed lines, live since 09-11) in
   favour of upstream's? Recommended: yes. It is upstream's maintained path, and the fork's
   round-14 passes mostly harden races that its own delegated model introduced.
2. G2: add the immediate rotation frame, or accept the window until `turn.end`?
3. G3: (a) accept, (b) the cheap interrupt-on-orphan shrink, or (c) full executor liveness?
   Codex held (c) as the production gate last time. Worth re-asking Codex, since the upstream
   model changes the exposure.
4. The Phase-0 renderer `[phase0]` logs: drop them, or keep them for the dropped-submit
   investigation (which found phase0 silent for that window)?
5. Cheap evidence for G1 before coding: a read-only count of `end_reason LIKE 'compute_host_%'` rows
   in `profiles\claudetriad\state.db`, which would show whether the fork's live code already
   prevents it. Say if you want that run.

## Risks

- **Ownership regressions on switching models**: §3a is fixed upstream, but upstream's fix is newer
  (09-17 for the rotation and close fixes) and has seen less soak than the fork's. Mitigation:
  salvage 4-6 scenarios from `test_compute_host_delegation_integration.py` and run them against
  upstream's shape: child re-claim, rotation fencing, close mid-turn, child restart keeps the row,
  stale vouch refused.
- **The A1 debounce widens G3's window**: the 3-confirmation parent guard lengthens the time before
  an orphaned child notices. This matters only if G3 is accepted.
- **Registry schema carry-over at deploy** (see step 5).

## G4 (added after Codex's review): a restarted host's version bumps are masked

Upstream behaviour, not reproduced live. A restarted compute host builds each session with
`history_version = 0` (`server.py:2479` via `compute_host.py:372`), while the parent holds N and
adopts `max(parent, frame)`. The child's next bumps (1, 2, ...) are ignored until they exceed N,
so a compression on the new host does not move the parent's version, and anything keyed on
version changes can miss it. The session_key rotation itself is still adopted, since it is
compared separately. Proposed fix: after `_init_session` on the normal path, seed
`session["history_version"]` from the frame, as the fallback already does. Failing-first test:
respawn the host, compress, and assert the parent's version increases. Belongs with the bucket C
gap fixes (G1-G3), in the same order and under the same decision.

## Implementation status (sync branch, bucket C)

| gap | commit | status |
|---|---|---|
| telemetry | 9f96207120 | ported slimmed: refusal names claimant + incumbent; child setup_logging() |
| G1 | 110d09031e | FIXED, red-first (child ended the parent's row for all 4 shutdown reasons) |
| G4 | 9d48f8e528 | FIXED, red-first (parent stayed at 7 after a respawned host compressed) |
| G2 | 94216610b4, e19e38f148 | upstream's lease fenced turn APPENDS only; compression COMMITS and a holder-less retry bypassed it (Codex review). FIXED, red-first. Registry-level residual R1 is a design choice |
| G3 | 94216610b4, e19e38f148, bd9fddabe7 | same fixes, plus ACP's non-owning transcript replace (R2 was a real destructive write). FIXED, red-first |

## G2 / G3 against Codex's bar: what upstream already guarantees (found while implementing)

The earlier G2/G3 rows reasoned about the **advisory** active-session registry
(`runtime/active_sessions.json`). Transcript writes are fenced by a different, durable mechanism
that the analysis did not account for: the **session turn lease** in state.db
(`session_turn_leases`, `agent/turn_facade_lease.py`, `hermes_state_compression.py`).

- It is keyed by the compression **lineage root** (`_session_turn_lease_key_on_conn`), so A and its
  continuation B share ONE lease row from the moment `publish_compression_child` links them, in the
  same transaction as the rotation. There is no instant at which B exists un-owned.
- Every turn write re-checks its holder **inside the write transaction**
  (`hermes_state_messages.py` ~228-245): a holder mismatch raises `SessionTurnLeaseLostError`, and the
  refresher (`DurableTurnLease.refresh_tick`, every 60s, TTL 300s) hard-interrupts the turn when the
  lease is lost. Failure stops the writer; it does not just log.
- In an isolated turn the CHILD's agent admits that lease (`admit_durable_turn_lease`, holder
  `pid=<child>`), so a successor after parent death cannot take the session while the child executor
  lives; once the child is gone its lease is reclaimable by the dead-pid rule, and a stalled child
  whose lease expired and was reclaimed is fenced on its next write.

The claim that this already met Codex's G2/G3 bar at the transcript-write level was WRONG for three
paths (Codex review of 4a6518a070, reproduced red-first before each fix):

1. **Compression commits bypassed the turn lease.** `publish_compression_child` and
   `archive_and_compact` checked only the compression lock, which stays valid independently of the
   turn lease: a taken-over executor could still publish a continuation or commit an in-place
   compaction over the successor. The pre-publication flush also swallowed failure.
   **Now (94216610b4, e19e38f148):** both commits run `_check_compression_turn_fence` inside the
   publication transaction (same rule and renewal as transcript writes). A turn's commit must still
   hold its lease; a turn-less commit is refused while another turn holds the conversation. Every
   in-turn caller passes its holder (rotation / in-place from the agent; proactive prune and
   micro-compaction via a weakref'd holder callback on the compressor). A recorded lease loss stops
   the rotation before publishing.
2. **A cleared holder authorized a late, unfenced retry.** After a refused flush, turn cleanup
   cleared `_active_session_turn_lease_holder`, and the shutdown flush retried the pending rows with
   `None`, which skips the lease check. **Now (e19e38f148):** the refused holder is recorded; until a
   new lease is admitted, a holder-less flush is fenced by the LOST holder, so the DB keeps refusing
   it. Re-admission under a new lease may persist pending rows (upstream's pinned contract; admission
   reloads the transcript after a wait).
3. **R2 hid a concrete destructive write.** ACP `_persist` from a non-owning agent (a second client
   after restore + model switch) replaced active messages from stale history with a DELETE and no
   lease check. **Now (bd9fddabe7):** metadata-only saves never touch messages; a non-owning
   transcript sync is lease-fenced in-txn (`reject_active_turn_lease`) and soft-archives dropped rows.

With those, Codex's G2 bar ("ownership of B established before any continuation write; failure stops
execution") and G3 bar ("takeover waits for the previous executor to stop, or it is fenced") hold for
turn appends, compression commits, late/shutdown flushes and ACP transcript syncs. Pinned by
`tests/hermes_state/test_turn_lease_spans_compression_rotation.py`,
`tests/hermes_state/test_compression_turn_lease_fence.py`,
`tests/agent/test_flush_after_turn_lease_loss.py` and
`tests/acp_adapter/test_session_persist_ownership.py`. Read and unit-tested against a real SessionDB,
not reproduced end-to-end through a live compute host.

### Residuals (open choices for Lake + reviewers)

- **R1: registry lag after a mid-turn rotation (UI ownership, not data).** Until `turn.end`, the parent's
  registry lease stays on A. A second window can claim B in the registry; its turn then WAITS on the
  DB lease ("Another Hermes process is using this session", up to 30 min) and runs afterwards. When
  the child's `turn.end` arrives, the parent's A->B re-anchor fails (B is held) and only logs, so two
  windows end up live on one conversation, taking turns serially. No transcript corruption.
  Options: (a) child emits a rotation frame at the rotation point and the parent re-anchors on the
  drain thread immediately (small-medium; narrows but cannot close the window, since the frame is
  asynchronous); (b) a synchronous child->parent handoff request that blocks the continuation until
  the parent confirms (medium-large; new request/ack frame pair on the host pipe, timeout policy, and
  it would duplicate a guarantee the DB lease already gives); (c) accept, documented.
  Recommendation: (c) or (a). (b) buys no data safety.
- **R2: non-turn transcript mutations.** Writes outside a turn (rewind, edits, archive) are fenced
  against an active turn lease only when the caller opts in (`reject_active_turn_lease`). The ACP
  path was a real hole (fixed, bd9fddabe7). A complete audit of every other `replace_messages` /
  mutation caller is still open.
- **R3: first turn of a brand-new session.** `admit_durable_turn_lease` skips the lease when no durable
  row exists yet ("a fresh session id has no durable transcript to race over"). Two runtimes racing
  the very first turn of the same new session are not serialized by it. Pre-existing upstream design.
- **R4: G3 detection latency** (parent-guard 3 confirmations + orphan drain up to 10s) still exists, but
  it bounds only how long an orphaned child keeps running, not whether its writes can interleave with
  a successor's (they cannot, per above).

## Open item (explicit): Yuanbao recall redaction

`gateway/platforms/yuanbao.py:530` (`_redact`) rewrites the WHOLE transcript (`rewrite_transcript(...,
active_only=True)`) without `reject_active_turn_lease`. Deferred because Lake does not use the
Yuanbao adapter, but it is NOT safe merely because redaction is a privacy action (Codex): a
whole-transcript rewrite can delete unrelated concurrent messages, and a stale writer could restore
recalled content. The eventual fix is a targeted, transactional redaction of the one recalled row,
fenced so stale writers cannot re-insert it. Stays open until fixed or the adapter is disabled.
