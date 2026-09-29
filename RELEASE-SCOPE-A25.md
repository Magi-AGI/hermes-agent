# A25 release scope: the turn and cancellation ownership contract, its inventory, and the disposition of every A25 finding

**DRAFT for review (not frozen).** Drafted 2026-09-28 by the A25 agent on branch `magi/track-a25-a19b`, tip `9f133723bd`. Round-10 code starts only after Codex and Antigravity review this list and Lake freezes it. After the freeze, a new discovery is classified by Part 3's outcomes and is not added to scope automatically; a demonstrated production bug is still fixed, disproved, or explicitly deferred by Lake. Format model: `RELEASE-SCOPE-A28.md` frozen at `adc7255836`.

## Baseline and rules

- **Code baseline.** Branch base `magi/sync-v2026.9.21` at `61dbdeb9e2`; A25 tip `9f133723bd`. Every file:line below was read at `9f133723bd`. Branch commits, oldest first: `5739719e1d` (A19b), `973107ce0b`, `b05b4b2f79` (A19b), `63bd2e97cc`, `ef77d8ac87`, `99ee48ecae`, `7b852732db`, `c4cad7957b`, `1192149410`, `5d1d8d85a0`, `4c4d2ace71`, `b9a4d0eccb`, `b1727b9aca`, `3b772a2cd5`, `b034536955`, `16e9c1dcb3`, `9f133723bd`.
- **What A25 is.** Fixes to existing code in `tui_gateway/`, plus the narrow hooks it needs in `tools/approval.py`, `tools/async_delegation.py`, `hermes_state_messages.py` and `run_agent.py`. **No general ownership framework is introduced.** The mechanisms already on the branch are the claim token, the cancellation registry, positive lease ownership and the held lease. Round 10 closes the listed gaps in those, row by row; the work list is the OPEN parts of Part 2.
- **Tests.** Every row has a pass/fail behavioural test: event-ordered (threading events and barriers, never sleeps as synchronisation), fake agents and a fake host, and real `SessionDB` and real active-session leases in temporary homes where the row concerns them. Tests run only through `scripts/run_tests_isolated.py -j 4`.
- **"Done" means** the named regression test passes at the named commit (and at `9f133723bd`). It does not mean release-validated. Combined-release validation (A28 I2, on the combined candidate) is a separate gate. Nothing in this document is marked release-validated.
- **Status values.**
  - `done @<commit>: <test>`: the row's property is met and the named test guards it.
  - `partly done`: the named test guards part of the property; the named OPEN part remains.
  - `OPEN (<findings>)`: work for round 10, with the test that must pass. A test that does not exist yet is either the reviewer's probe to adopt (named as in `review-artifacts/a25-9f133723bd/test_a25_round9_edges.py`) or a new test named here.
  - `BASELINE`: behaviour that predates A25, has no A25 finding and gains no A25 property; listed so the inventory is complete.
  - `excluded`: out of A25 with the reason given.
- **Finding ids.** `R<round>-<reviewer>-<id>`, reviewer `CX` (Codex) or `AG` (Antigravity). A28 rows are `A28-H9` and `A28-I1`.

## Part 1: the contract

Each rule is a behaviour property. Each Part 2 row lists the rules it must satisfy.

- **(A) One claim per turn.** Every turn, whatever started it, holds exactly one claim from acceptance until it settles. Every effect of that turn happens only while that claim is still the session's, compared by identity at the moment of the effect. That covers its start, its dispatch to the compute host, every durable write, every event it publishes and every release. No effect is decided by a bare `running` flag, by the absence of a claim, or by an observation taken before the effect. A claim that has not started is revoked by the Stop that observed it. A started claim is released only by its own turn's settlement.
- **(B) Every cancellation is registered and bound to its target.** A cancellation (Stop, interrupt, redirect, barge-in, takeover, reset) is registered before it publishes anything and stays registered until its last effect has run. Its target is chosen and its registration made in one step. Every effect it has applies only to that target: flags, generations, queue changes, claim revocation, markers, and the waits it releases. A turn admitted while a registered cancellation is still running waits for it, visibly and without a deadline (Lake's decision), and a Stop aimed at the waiting turn ends it.
- **(C) Only the current owner acts on the conversation.** An effect on the whole stored conversation needs positive, current, per-profile ownership at the moment of the effect. Such effects are the delegation selectors, the approval queue and callback, the end of the durable row, and the lease itself. The lease is released last: after every such effect, after the owner's own and adopted cancellations settle, and after the owner's turn body has exited. A lease that cannot be released keeps an owner and a retry path.
- **(D) Exactly-once, correctly attributed outcomes.** Every turn ends in exactly one terminal outcome, attributed to that turn and never observable as the outcome of a successor. A turn cancelled before it starts runs nothing: no input preparation and no model call. Every durable effect (user row, branch seed, crash marker, delivery acknowledgement, loop tick, mailbox receipt, row end) is applied exactly once and stays correct under transaction retry, partial commit and failure. A retried or failed write never publishes success it did not commit and never duplicates what it did commit.

(A)–(C) are as given to A28 for its H rows. (D) states the outcome rules the reviews have enforced since round 7 (Stop-to-skip, the user-row and seed fixes). It is written down here for the first time and is listed under Needs Lake (N1).

## Part 2: the inventory

Enumerated from the source at `9f133723bd`. Paths without a directory are under `tui_gateway/`.

**Completeness is claimed only as far as the census in 2.8 proves it,** that is, for the entry points it drives and the effect sinks it wraps. An independent source audit is invited to find what the census misses. Every item it finds becomes a row before the freeze, or is dispositioned per Part 3 after it.

### 2.1 Turn sources (T)

| row | source | code | rules | test | status |
|---|---|---|---|---|---|
| T1 | `prompt.submit`, in-process | claim `methods_prompt.py:580` (`_lock_in_submit_turn` :548); worker thread :730-757; `_run_after_agent_ready` :496; run `prompt_turn.py:982` | A, D | a revoked submit never runs, releases or publishes inside its successor | partly done @9f133723bd: `test_revoked_ordinary_submit_cannot_enter_successors_turn`, `test_submit_worker_start_failure_releases_its_claim`, census rows `run_after_agent_ready`, `admit_prompt_turn`, `run_prompt_submit`. OPEN: publication (P9) |
| T2 | `prompt.submit` routed to the compute host | `methods_prompt.py:701`; `compute_host_bridge.py:294` | A, D | a revoked submit dispatches no frame | done @9f133723bd: `test_a_revoked_submit_is_not_dispatched_to_the_compute_host`, census `compute_host_dispatch` |
| T3 | queued-prompt drain, in-process | claim `session_auto_continue.py:630`; `_dispatch_drained_prompt` :651, run :662 | A, D | a drain revoked before or after admission never starts; its refusal or exception never releases a successor | done @9f133723bd: `test_drain_revoked_after_admission_cannot_start_after_successor`, `test_exception_from_revoked_drain_cannot_release_successor`, `test_an_old_drains_refusal_never_releases_a_newer_drains_claim[owner|host|generation]` |
| T4 | queued-prompt drain routed to the host | `session_auto_continue.py:665` | A, B, D | as T2, for a drained prompt | partly done @9f133723bd: census `compute_host_dispatch` (shared path). OPEN: X7 (R9-CX-F18) |
| T5 | crash auto-continue | `_maybe_schedule_auto_continue` `session_auto_continue.py:58`, claim :96, run :120 | A, D | a Stop before the continuation starts cancels it; its release is by identity | OPEN (shared start gate only). New test: `test_auto_continue_revoked_before_start_runs_nothing` |
| T6 | goal follow-up | `prompt_turn.py:506-507` → `_dispatch_followup_turn` :473 | A, D | as T5 | OPEN. New test: `test_followup_revoked_before_start_runs_nothing[goal]` |
| T7 | post-turn follow-up (slash loop returning a prompt) | `_run_post_turn_followups` `prompt_turn.py:490`; `_dispatch_followup_turn` :473, :479 | A, D | as T5 | OPEN. New test: `test_followup_revoked_before_start_runs_nothing[slash-loop]` |
| T8 | slash `/loop` tick | `_notif_slash_loop_tick` `session_notifications.py:179`, claim :192, send :197 | A, D | a Stop before the body starts pauses the loop (Lake); a refusal leaves it due | partly done @63bd2e97cc: `test_a_loop_wakeup_refused_before_its_turn_starts_stays_due`. OPEN: Stop before start (P11, R9-CX-F19) |
| T9 | heartbeat tick | `_maybe_fire_tui_heartbeat_tick` `session_notifications.py:228`, claim :247, run :255 | A, D | a revoked heartbeat claim runs nothing and releases nothing else | partly done @9f133723bd: census `notif_release_turn`. OPEN: new test `test_heartbeat_revoked_before_start_runs_nothing` |
| T10 | loop wake-up | `_maybe_fire_tui_loop_tick` `session_notifications.py:274`, claim :286, run :299 | A, D | the tick settles with its turn; a Stop before start pauses | partly done @63bd2e97cc: `test_a_loop_wakeup_that_does_not_complete_settles_its_tick[stopped|failed]`, `test_a_loop_wakeup_whose_turn_compresses_settles_the_continuations_loop[complete|stopped|failed]`. OPEN: Stop before start (R9-CX-F19) |
| T11 | kanban notification batch | `_notif_poll_kanban_scoped` `session_notifications.py:435`, claim :449, submit :459 | A, D | a refused batch is kept, not dropped | OPEN (R9-CX-F20 class, P12). New test: `test_kanban_batch_refused_is_kept_pending` |
| T12 | background notification event | `_notif_handle_event` `session_notifications.py:496`, claim :542, `_notif_dispatch_event` :464, submit :488 | A, D | a refused turn is not acknowledged | OPEN (R9-CX-F20): `test_notification_refused_for_foreign_lease_is_not_marked_delivered` |
| T13 | completion batch | `_notif_dispatch_completions` `session_notifications.py:552`, claim :558, submit :581 | A, D | as T12 | OPEN (R9-CX-F20). New test: `test_completion_batch_refused_is_not_marked_delivered` |
| T14 | bot mailbox DM | `_poll_bot_live_delivery_once` `session_notifications.py:607`, claim :633, run :650 | A, D | exactly one receipt, for a turn that ran or was refused | partly done @9f133723bd: `test_bot_live_owner_delivery.py::test_refused_input_commits_failed_mailbox_receipt`. OPEN: new test `test_mailbox_turn_revoked_before_start_commits_one_receipt` |
| T15 | compute-host child `turn.start` | `compute_host.py:214-215` (executor), `_run_real_turn` :286, claim :313, run :327 | A, B, D | see HB1, HB5 | partly done @9f133723bd: `test_a_compute_host_stop_frame_still_propagating_holds_the_hosts_next_turn`. OPEN: A28-I1 (HB5) |
| T16 | hosted room member submit | `prompt.submit` with `_hosted_task` `methods_prompt.py:193`, :625-627 | A, D | as T1; its Stop is bound to its task (X3) | partly done @9f133723bd via T1. OPEN: X3 |
| T17 | side question `prompt.btw` | separate agent and history | — | — | excluded: not a primary turn; it runs on its own agent and holds no claim on the session (Codex R7 AUDIT agrees) |

### 2.2 Cancellation sources (X)

| row | source | code | rules | test | status |
|---|---|---|---|---|---|
| X1 | Stop (`session.interrupt`) | `methods_session.py:2109-2146` → `_interrupt_session_turn` `session_lifecycle.py:665`, `_interrupt_session_turn_now` :688 | A, B | registered for its whole propagation; revokes only the unstarted claim it observed; every effect (cancel flag, `_cancel_generation`, queue drop) applies only to its observed target; an idle Stop never revokes a later claim | partly done @5d1d8d85a0 / @9f133723bd: `test_a_cancellation_still_propagating_never_touches_the_next_turns_work[stop]`, `test_old_stop_cannot_revoke_the_claim_that_started_waiting_for_it`, census `stop_tail_revoke`. OPEN (R9-CX-F04): `test_old_stop_before_flag_publication_does_not_cancel_the_waiting_successor`, `test_idle_stop_does_not_revoke_a_later_started_claim` |
| X2 | second Stop while the next turn waits | same path; `_settle_stale_busy_interrupt` `session_auto_continue.py:461` | B, D | ends the waiting turn without preparation, also at the last-completion instant; deadlocks nothing | done @b1727b9aca / @b034536955 / @9f133723bd: `test_a_second_stop_while_the_next_turn_waits_stops_that_turn_and_deadlocks_nothing`, `test_stop_to_skip_cancellation_wait_never_enters_input_preparation`, `test_stop_at_the_last_cancellation_completion_still_skips_input_preparation` |
| X3 | hosted-task Stop (`expected_hosted_task_id`) | `methods_session.py:2115-2135`; check `session_lifecycle.py:688-700` | B | stops only the checked task; a refused Stop publishes no interrupt intent | partly done @9f133723bd: `test_hosted_task_stop_remains_bound_to_the_checked_task`. OPEN (R9-CX-F17): `test_refused_hosted_stop_does_not_preserve_an_older_interrupt` |
| X4 | client-gone reaper | `_schedule_ws_orphan_reap` `session_lifecycle.py:919`, :978 | B | as X1 | done @b9a4d0eccb: `test_a_cancellation_still_propagating_never_touches_the_next_turns_work[client-gone]` |
| X5 | lease-takeover Stop of the old runtime | `_take_over_detached_runtime_lease` `session_lifecycle.py:165`, handoff :191, Stop :199 | B, C | the new owner's turns wait for the old Stop, for every cancellation it adopted, and for the old turn body to exit | partly done @b1727b9aca / @3b772a2cd5: `test_a_lease_takeovers_stop_of_the_old_runtime_ends_before_the_new_runtimes_turn_starts`, `test_a_second_submit_cannot_use_a_taken_over_lease_before_the_old_runtimes_stop_settles`, `test_a_takeover_inherits_every_cancellation_still_using_the_stored_session[existing-stop|two-hops]`. OPEN: the body fence (L12, R9-CX-F09) |
| X6 | busy input, in-process interrupt worker and its deferred deliveries | `_handle_busy_submit` `session_auto_continue.py:547`; `_claim_busy_interrupt` :370; `_interrupt_busy_session` :394-446; launch :598 | B, C, D | never reaches a later turn; its completion runs the common settlement, which releases a held lease; a launch failure does not leave accepted work silently queued | partly done @ef77d8ac87 / @c4cad7957b: `test_a_busy_interrupt_never_reaches_the_turn_of_the_prompt_it_queued`, `test_an_earlier_turns_busy_interrupt_never_escapes_to_the_next_turn[wait-expires|second-busy-input|stop]`, `test_an_earlier_turns_busy_interrupt_never_cancels_the_next_turns_work`, `test_a_deferred_busy_interrupt_still_propagating_never_reaches_the_turn_after_a_stop`. OPEN (R9-CX-F12): `test_busy_worker_completion_releases_the_lease_held_for_it`; (R9-CX-F21): `test_busy_interrupt_launch_failure_does_not_hide_accepted_work` |
| X7 | busy input routed to the host (targeted soft interrupt) | `session_auto_continue.py:409-414` | B | a deferred intent is kept for the actual next host turn | OPEN (R9-CX-F18): `test_busy_host_interrupt_keeps_an_intent_for_the_next_host_turn[True]` (control `[False]`) |
| X8 | redirect: busy-input default and `session.redirect` | `_apply_turn_correction` `session_auto_continue.py:509`, :518; `methods_session.py:2155` | B | the target is chosen and registered in one step; never aborts a successor's request | done @b034536955 / @9f133723bd: `test_redirect_propagation_cannot_abort_successors_request[busy-redirect|session-redirect]`, `test_correction_selected_for_old_turn_does_not_hit_the_successor[redirect]` |
| X9 | voice barge-in | `_fd_trip` `methods_voice.py:219`, :239-252 | B | as X8; a barge-in aimed at the waiting turn survives the older settle | done @b1727b9aca / @9f133723bd: `test_an_interrupt_aimed_at_the_waiting_turn_survives_the_older_cancellation_settling[voice-barge-in]`, `test_a_voice_barge_in_still_propagating_holds_the_turn_after_the_one_it_stopped`, `test_correction_selected_for_old_turn_does_not_hit_the_successor[voice]` |
| X10 | compute-host Stop frame (child) | `_handle_interrupt` `compute_host.py:232`, :259-262 | B | as X1, inside the child | done @b9a4d0eccb: `test_a_compute_host_stop_frame_still_propagating_holds_the_hosts_next_turn` (the I1 overtaking case is HB5) |
| X11 | compute-host soft interrupt (child, `turn_id`) | `compute_host.py:240-257` | B | registered, and fenced to its turn | done @b034536955: `test_compute_host_soft_interrupt_cannot_propagate_into_queued_successor` |
| X12 | compute-host shutdown interrupting live turns | `_interrupt_live_turns` `compute_host.py:150-162` | B | as X1 | OPEN (no finding; no test). New test: `test_host_shutdown_stop_is_registered_and_bound` |
| X13 | close settling an isolated turn | `_settle_isolated_turn_before_close` `session_lifecycle.py:609-626` | B, C | as X1; the lease is retained until the child settles | OPEN (no finding; the retention predates A25 and has no A25 test). New test: `test_close_of_isolated_turn_keeps_lease_until_host_settles` |
| X14 | agent reset (`/new`, `tools.configure`) | `_reset_session_agent` `agent_callbacks.py:523`; `_rebuild_session_agent` :482; caller `methods_tools.py:1113` | A, B | publishes the agent, queues, overrides and lifecycle only against an unchanged, exclusive state; never replaces a later turn's agent or history (including the idle-to-idle case); never releases a started turn | partly done @b034536955 / @9f133723bd: `test_a_reset_agents_first_turn_waits_for_the_old_agents_stop`, `test_agent_reset_cannot_publish_over_a_turn_admitted_while_rebuilding`. OPEN (R9-CX-F03): `test_reset_cannot_replace_a_new_turns_agent_or_erase_its_history[False|True]`, `test_reset_cannot_release_a_started_turn_before_its_body_exits` (control `test_control_reset_of_idle_unchanged_session_succeeds`) |
| X15 | parent Stop sent to the host | `session_lifecycle.py:700-706` | B | see HB5–HB7 | OPEN (A28-I1) |
| X16 | compression re-key invalidating a drain claim | `_release_invalidated_drain_claim` `session_auto_continue.py:603` | A | the claimed prompt is re-queued, never lost or run twice | done @63bd2e97cc: `test_a_drain_claim_a_compression_invalidated_still_runs_its_prompt` |
| X17 | subagent interrupt | `methods_subagents.py:84` | — | — | excluded: targets a child agent, not the session's turn (Codex R7 AUDIT) |
| X18 | typed stop phrase | `_typed_stop_phrase_response` `methods_prompt.py:169` | — | — | excluded: ends voice chat and cancels no turn |

### 2.3 Publication points (P)

| row | effect | code | rules | test | status |
|---|---|---|---|---|---|
| P1 | `message.start` | `prompt_turn.py:1030`; `session_notifications.py:166`, :297; `session_auto_continue.py:118` | A, D | a start is published only for a claim that then either starts or is closed by an outcome attributed to it | OPEN (R9-CX-F15) |
| P2 | start hand-off (turn thread) | `prompt_turn.py:1128-1153` | A | a revoked claim starts nothing; a failed start releases its claim | done @9f133723bd: census `start_handoff`, `test_drain_revoked_after_admission_cannot_start_after_successor`, `test_submit_worker_start_failure_releases_its_claim` |
| P3 | host dispatch | `compute_host_bridge.py:303-317` | A | see T2 | done @9f133723bd: as T2 |
| P4 | in-flight record | start `methods_prompt.py:585`, `prompt_turn.py:162`, `compute_host.py:315`; clear on release | A | written and cleared only under the owner's claim | done @9f133723bd: census `test_no_path_clears_or_enters_another_owners_turn_claim` (asserts the successor's record is untouched on every row) |
| P5 | submit-time user row | `_persist_submit_user_row` `session_workdir.py:376`, called `methods_prompt.py:462` | A, D | a submit revoked before its row commits never writes it, in either order; the row is staged only for the current claim | done @16e9c1dcb3 / @9f133723bd: `test_a_revoked_submit_never_leaves_its_user_row_in_the_stored_transcript[successor-writes-first|revoked-resumes-first]`, census `branch_seed_and_user_row`. Retry: E4 |
| P6 | branch seed | `_persist_branch_seed` `session_workdir.py:319`; callers `methods_prompt.py:461`, `compute_host.py:324`, `methods_session.py:291` | A, D | written once; never after a revoked submit | partly done @9f133723bd: `test_a_revoked_branch_submit_cannot_append_its_seed_after_the_successor`. OPEN: E1, E2 |
| P7 | crash marker record | `_record_turn_marker` `prompt_turn.py:200`, called :1040 | A, D | the marker belongs to its turn | BASELINE: `test_auto_continue.py::test_interrupt_racing_marker_write_cannot_leave_recovery_state` |
| P8 | crash marker retire | turn tail `prompt_turn.py:1104-1110`; Stop RPC tail `methods_session.py:2140-2145`; terminal error `session_auto_continue.py:745`; recovery path `prompt_turn.py:871` | A, D | only the turn's own marker is retired, never after a successor wrote one for the same stored key | partly done @9f133723bd (Stop tail): `test_stop_rpc_tail_cannot_remove_a_successors_crash_marker`. OPEN (R9-CX-F14, F16): `test_terminal_error_cannot_publish_after_its_owner_is_replaced`, `test_old_turns_post_release_tail_preserves_new_turns_crash_marker` |
| P9 | terminal and error events | `message.complete` `prompt_turn.py:1051`, :1068, :1152; `_emit_terminal_turn_error` `session_auto_continue.py:715-747`; refusals `prompt_turn.py:137`, :145, :174; readiness `methods_prompt.py:510-532`; cancelled before start `methods_prompt.py:767`, `compute_host_bridge.py:316`; drain `session_auto_continue.py:670`; host `compute_host_bridge.py:270`; exceptions `prompt_turn.py:897`, :904 | A, D | published only while the claim is current, or carrying an identity the client checks; never lands on a successor's stream; a refusal publishes before it releases | partly done @9f133723bd (the in-flight record): `test_late_agent_ready_failure_does_not_fail_the_successors_inflight_record`, `test_a_missing_agent_refusal_fails_only_its_own_turn`, census `terminal_error`. OPEN (R9-CX-F14, F15): `test_terminal_error_cannot_publish_after_its_owner_is_replaced`, `test_revoked_submit_never_sends_unscoped_terminal_event_to_successor[before-start|ready-success|start-gate]`, `test_synthesized_refusal_does_not_leave_a_successors_idle_marker` |
| P10 | turn-end tail | `prompt_turn.py:1084-1114`: last_active, in-flight clear, hosted slot, marker, `_hosted_room_task`, `_auto_continue_scheduled`, settled session info | A, D | every owned effect runs before the claim is released, or compares the turn's own identity | OPEN (R9-CX-F16), see E3 |
| P11 | loop tick settlement | `_settle_loop_wakeup` `prompt_turn.py:453`, called :1079; `abandon_tick` `session_notifications.py:193`, :300, :305 | D | a stopped wake-up pauses the loop (Lake); a refused one stays due | partly done @63bd2e97cc (turn ran). OPEN (R9-CX-F19): `test_stop_before_loop_body_starts_pauses_instead_of_refiring` |
| P12 | notification delivery acknowledgement | `complete_event_delivery` / `release_event_delivery` `session_notifications.py:487-493`, :571-590 | D | only a turn that started is acknowledged; a refused one is released for redelivery | OPEN (R9-CX-F20): `test_notification_refused_for_foreign_lease_is_not_marked_delivered` (control `test_control_notification_started_is_acknowledged`) |
| P13 | mailbox receipt and hosted terminal callback | `session_notifications.py:637-660`; `prompt_turn.py:865`, :891, :1057 | D | exactly one receipt per delivery | partly done: `test_refused_input_commits_failed_mailbox_receipt`. OPEN: T14 |
| P14 | the agent's own transcript writes during the body | agent flush (`agent/session_persistence.py`, DB turn lease) | A, C | no write by a body whose runtime has lost ownership | OPEN via L12 (R9-CX-F09) |
| P15 | finalize persist | `session_lifecycle.py:407-411`; `session_reaper.py:52` | C | runs while the lease is held | done @9f133723bd: `test_close_holds_the_lease_through_its_stored_conversation_effects` |
| P16 | approval request and response | emit `server.py:762`; resolve `server.py:788`; `approval.respond` `methods_prompt.py:1314-1330` | C | resolves only the owning profile's conversation | OPEN (R9-CX-F08, O8) |
| P17 | host completion adoption | `compute_host_bridge.py:249`, :259-291, :335 | A | settles only the claim it was dispatched under | done @9f133723bd: `test_host_completion_settles_only_the_claim_it_was_dispatched_under` |
| P18 | waiting notice (status line) | `_settle_stale_busy_interrupt` `session_auto_continue.py:461`; desktop `gateway-event/status.ts` | D | shown after 2 s and cleared when the wait ends; a ready frame clears only its own notice | done @99ee48ecae / @1192149410: `test_a_turn_waiting_on_an_earlier_cancellation_says_so_and_clears_it`; desktop `status-notice-event.test.tsx` ("shows a waiting notice…", "leaves a provider wait shown…") |

### 2.4 Conversation-wide ownership decisions (O)

| row | decision | code | rules | test | status |
|---|---|---|---|---|---|
| O1 | Stop's stored-conversation delegation selector | `session_lifecycle.py:736-747` | C | positive, per-profile owner only | done @b034536955 / @9f133723bd: `test_a_retired_runtimes_later_stop_ends_only_its_own_delegations`, `test_runtime_that_never_owned_lease_cannot_stop_owners_delegations`, `test_owner_stop_is_scoped_to_its_profile_not_a_copied_session_id` |
| O2 | finalize's delegation selector | `session_lifecycle.py:452-459` | C | runs while the lease is held, on every close path | partly done @9f133723bd (manual close): `test_close_does_not_use_ownership_from_before_releasing_the_lease[delegation]`, `test_close_holds_the_lease_through_its_stored_conversation_effects`. OPEN (R9-CX-F05, F06): `test_close_keeps_lease_through_conversation_delegation_cleanup[False|True]` |
| O3 | key-wide approval deny | `_release_turn_waits` `session_lifecycle.py:796-810`; host `compute_host.py:254` | C | current, per-profile owner only | partly done @b034536955 (same profile): `test_retired_runtime_stop_cannot_deny_successors_approval`. OPEN (R9-CX-F08): `test_conversation_approval_cleanup_cannot_cross_profiles[stop]` |
| O4 | approval callback and queue removal | teardown `session_lifecycle.py:512-521`; late build `server.py:1072-1077`; compression `session_compression.py:285-291` | C | the owner removes only its own callback; the queue is drained only for its own profile | partly done @b034536955 / @9f133723bd: `test_a_non_owning_runtimes_close_leaves_the_owners_approvals_and_callback`, `test_the_owners_close_removes_only_its_own_approval_callback`, `test_close_does_not_use_ownership_from_before_releasing_the_lease[approval]`. OPEN (R9-CX-F08): `test_conversation_approval_cleanup_cannot_cross_profiles[close]` |
| O5 | end of the durable row | finalize `session_lifecycle.py:430-447`; `agent.close()` → `run_agent.py:1027-1035` (`_end_session_on_close`, `session_lifecycle.py:451`) | C, D | ended once, by the owner, before the lease is released; the late agent close never re-ends a row another owner reopened | partly done @9f133723bd (in-process viewer): `test_viewer_close_cannot_end_the_current_owners_database_row`. OPEN (R9-CX-F07): `test_agent_close_tail_cannot_reend_the_new_owners_reopened_row` |
| O6 | automatic-desktop lifecycle guard | `_other_runtime_lease_guard` `session_lifecycle.py:273-310` | C | the guard's release does not come before the conversation-wide cleanup | OPEN (R9-CX-F05) |
| O7 | non-owner close: is there a live owner? | `_live_owner_in_process` `session_lifecycle.py:762-772`, used :434-436 | C | consults every live lease for the key, not only this backend's | OPEN (R9-CX-F10): `test_viewer_close_checks_an_owner_outside_this_backends_session_map` |
| O8 | approval namespace | `tools/approval.py:117-165` (queues and callbacks keyed by the bare stored id) | C | registration, lookup, resolution and removal are scoped by profile plus conversation | OPEN (R9-CX-F08); Needs Lake N4 |
| O9 | approval callback registration | `server.py:1007`; `_approval_notify_for` :991 | C | as O8 | OPEN (R9-CX-F08) |

### 2.5 Lease and claim handoffs (L)

| row | handoff | code | rules | test | status |
|---|---|---|---|---|---|
| L1 | lease acquisition at the first turn | `_ensure_active_session_slot` `session_lifecycle.py:128-144` | C | — | BASELINE (the census drives it; no A25 property added) |
| L2 | lease takeover | `session_lifecycle.py:165-204` | B, C | see X5 | as X5 |
| L3 | close: hold for own and adopted cancellations, then release | `_teardown_popped_session` :560-578; `_hold_lease_for_cancellations` :581-598; release :419, :463 | B, C | released only after the cancellations settle **and** the closing owner's conversation-wide effects finish | partly done @b034536955 / @9f133723bd: `test_close_keeps_outstanding_cancellation_owned_until_it_settles`, `test_inherited_cancellation_is_not_lost_when_the_intermediate_owner_closes`. OPEN (R9-CX-F06): `test_close_keeps_lease_through_conversation_delegation_cleanup[True]` |
| L4 | held-lease release on settlement | `_close_cancellation` `session_auto_continue.py:344-356`; holder map :340 | B, C | a settlement cannot be missed (holder registration versus the last completion), and every completion, including the busy worker's, uses the common protocol | OPEN (R9-CX-F11, F12): `test_last_adopted_cancellation_cannot_finish_before_holder_registration`, `test_busy_worker_completion_releases_the_lease_held_for_it` (control `test_control_registered_holder_releases_after_last_cancellation`) |
| L5 | isolated-turn deferred lease | `session_lifecycle.py:609-626`; release `compute_host_bridge.py:286-290`, `_release_deferred_lease` `session_lifecycle.py:631` | C | see E5 | OPEN via E5 |
| L6 | compression re-key lease transfer | `_transfer_active_session_slot` `session_lifecycle.py:309-322` | C | — | BASELINE: `test_protocol.py::test_sync_session_key_after_compress_reanchors_active_session_lease` |
| L7 | borrowed lease in the host child | `_install_borrowed_lease` `session_lifecycle.py:88`; `compute_host.py:301` | C | the child never ends the parent's row | BASELINE: `test_compute_host_borrowed_lease.py`, `test_compute_host_shutdown_preserves_parent_row.py` |
| L8 | Stop's claim revocation | `session_lifecycle.py:748-757` | A, B | see X1 | as X1 |
| L9 | drain claim re-queue on invalidation | `session_auto_continue.py:603-617` | A | see X16 | done: as X16 |
| L10 | reset's claim | `agent_callbacks.py:523-557` | A | see X14 | OPEN: as X14 |
| L11 | claim start hand-off | `_start_turn_claim` `session_auto_continue.py:258` | A | see P2 | done: as P2 |
| L12 | old executing body versus the new owner | close join grace `session_lifecycle.py:564-577`; takeover :165-204 | C | the lease, and a takeover's handoff, is not given away while the old body can still act; a completed interrupt is not that boundary | OPEN (R9-CX-F09): `test_close_grace_expiry_does_not_give_away_a_still_running_turns_lease`, `test_takeover_keeps_old_body_fenced_until_it_settles`. Needs Lake N2 |

### 2.6 Compute-host boundary (HB)

| row | item | code | rules | test | status |
|---|---|---|---|---|---|
| HB1 | `turn.start` admission in the child | `_handle_turn_start` `compute_host.py:214-216`; `_run_real_turn` :286-360 | A | claimed by identity; releases only its own claim | done @9f133723bd: `test_a_compute_host_stop_frame_still_propagating_holds_the_hosts_next_turn` |
| HB2 | soft interrupt | `compute_host.py:240-257` | B | as X11 | done: as X11 |
| HB3 | hard Stop frame | `compute_host.py:259-262` | B | as X10 | done: as X10 |
| HB4 | **A28-H9** control head-of-line blocking | the reader handles every frame inline (`compute_host.py:725-745`); `_handle_control` :508-530 runs compress inline via `methods_session.py:1952-1974`; `_handle_reload_mcp` :500 | B | a long control for session A does not delay `interrupt`, `respond` or `turn.start` for other sessions, or Stop for A, beyond a bound | OPEN (A28-H9). Acceptance (from A28): a fake compress blocks for 30 s; an `interrupt` for another session is applied in < 1 s; a `respond` is delivered within the parent's 15 s wait. New test: `test_long_control_does_not_block_other_frames` |
| HB5 | **A28-I1** a Stop overtakes a queued `turn.start` | `turn.start` goes onto the executor (`compute_host.py:215`); `interrupt` runs inline on the reader (:232-262); the claim resets the cancel flag (:313-314) | A, B | a Stop delivered between the host's receipt of `turn.start` and its claim ends that turn `interrupted` without calling the model | OPEN (A28-I1). New test: `test_stop_between_turn_start_receipt_and_claim_cancels_that_turn` |
| HB6 | **A28-I1** a soft interrupt carrying an old `turn_id` | `compute_host.py:240-257` | B | never applied to the next turn | done @b034536955 (X11). To re-run as I1 acceptance: `test_soft_interrupt_with_old_turn_id_is_not_applied_to_next_turn` |
| HB7 | **A28-I1** a Stop sent to a respawned host without the session | parent `session_lifecycle.py:700-706` (no `turn_id`, no ack check); child `compute_host.py:233-236` (`applied=False`) | B, D | the Stop is not lost silently: the parent's turn ends cancelled, or the client is told it was not applied | OPEN (A28-I1). New test: `test_stop_to_host_without_the_session_is_not_lost`. Needs Lake N5 |
| HB8 | host completion | `_on_compute_host_turn_done` `compute_host_bridge.py:259-291` | A | releases only its own claim | done: as P17 |

### 2.7 Effect lifetime (E): retries, partial commits, cleanup after release, late notifications, failed release

| row | item | code | rules | test | status |
|---|---|---|---|---|---|
| E1 | seed write retried by `_execute_write` | `session_workdir.py:337-364`; `hermes_state_messages.py:400-411`; `hermes_state.py:945-1041` | D | success is published only from a committed write; a retried transaction neither loses nor duplicates rows | OPEN (R9-CX-F01, a round-9 regression): `test_branch_seed_survives_retry_after_precondition` (control `test_control_uncontended_seed_is_written_once`) |
| E2 | seed partly committed across chunks | `session_workdir.py:357-368`; `hermes_state_messages.py:388-399` | D | a retry resumes without duplication; a submit never continues on an incomplete seed | OPEN (R9-CX-F02): `test_partial_branch_seed_can_resume_without_duplication` |
| E3 | cleanup after the claim is released (turn tail) | `prompt_turn.py:1084-1114` | A, D | see P10 | OPEN (R9-CX-F16) |
| E4 | user-row write retried | `session_workdir.py:376-410`; `append_message` precondition `hermes_state_messages.py` | D | a retried transaction writes the row once or not at all, and stages it only if it committed | OPEN (no finding; the same retry class as E1). New test: `test_user_row_survives_retry_after_precondition` |
| E5 | deferred or held lease release fails | `_close_cancellation` `session_auto_continue.py:351-355`; `_release_deferred_lease` `session_lifecycle.py:631-638`; isolated-turn release `compute_host_bridge.py:286-290` | C | a failed release keeps an owner and a bounded retry until it succeeds, and stays in `_own_live_lease_ids` | OPEN (R9-CX-F13): `test_failed_deferred_release_keeps_authority_for_retry` |
| E6 | late terminal events after the owner changed | see P9 | A, D | see P9 | OPEN (R9-CX-F14, F15) |
| E7 | late notification or delivery acknowledgement | see P12 | D | see P12 | OPEN (R9-CX-F20) |
| E8 | late agent-close tail | see O5 | C, D | see O5 | OPEN (R9-CX-F07) |
| E9 | the lease release at an ordinary close fails | `_release_active_session_slot` `session_lifecycle.py:221-233`, called :419, :463 | C | as E5, for an ordinary close | OPEN (no finding; the same class as E5). New test: `test_failed_close_release_keeps_authority_for_retry` |

### 2.8 Completeness census

- **Existing (done @9f133723bd):**
  - `test_no_path_clears_or_enters_another_owners_turn_claim` has 14 rows: start hand-off, Stop tail, terminal error, host dispatch, seed and user row, claim release, refused-claim release, notification release, rejected-submit release, readiness, admission, run, drained-prompt exception, and notification submit failure.
  - `test_conversation_ownership_is_positive` includes per-profile rows.
  - Mutation evidence for both is in the round-9 report.
  - Codex R9 F22 shows they do not cover guard-to-emission gaps, cleanup after release, retry callbacks, foreign-backend visibility, or registration-versus-completion ordering.
- **Round-10 addition: `test_every_observed_effect_maps_to_an_inventory_row`.** Behavioural, not source-reading.
  - **What it drives.** Every in-scope T row through acceptance → settlement, and every in-scope X row through registration → completion. It uses fake agents, a fake host, and a real `SessionDB` and real leases in temporary homes.
  - **What it wraps.** The effect sinks:
    - `server._emit`;
    - the `SessionDB` write methods `append_message`, `append_messages_batch`, `end_session` and `replace_messages`;
    - turn-marker record and clear;
    - `tools.approval` register, drop, unregister, resolve and withdraw;
    - `tools.async_delegation.interrupt_for_session`;
    - lease acquire, release and transfer;
    - event-delivery complete and release, and mailbox `complete_delivery`;
    - the loop manager's tick methods;
    - the host supervisor's submit, interrupt, control and respond.
  - **What it checks.** For each sink call it records the calling function, as observed at run time. It fails on any (sink, caller) pair that is not mapped to a row id above. Each mapped row is then driven twice at its recorded boundary: with the owner current, the effect happens; with the owner revoked or replaced at that boundary, it does not.
- **Round-10 extension of the existing census:** rows for P1 (start emit), P8 (turn-tail marker), P9 (each terminal emitter), P10 (tail pops), P11, P12, E1 and E4 (retry callback), O7 (foreign-backend lease), L4 (holder registration versus completion), and X14 (agent replacement and queues).
- **What the census proves, and what it does not.** It proves that every effect published through a wrapped sink by a driven source maps to a row and is checked for ownership at that row's boundary. It does not prove that no other entry point or sink exists. An effect through an unwrapped sink, or from a source the census does not drive, escapes it. **Reviewers are asked to audit the source independently for such entry points and sinks;** each one found becomes a row.

### 2.9 A28 H-row conformance (checked against Part 1)

Rows H1–H8 and H10–H12 of `RELEASE-SCOPE-A28.md` @`adc7255836` were read against rules A–D. **No conflict was found.** Dependencies to keep:
- H2 and H6: the inline fallback re-uses the turn's existing claim. Host dispatch has already marked that claim started, so the in-process start gate accepts it, and a Stop interrupts the turn rather than revoking the claim.
- H6: the "uncertain" outcome keeps the claim with the host turn until it settles. This matches rule A.
- H11: it retires only the request it observed, compared by identity. This matches rule A.
- H8: late control operations keep their owner. This matches rule B.

H9 and I1 are HB4–HB7 above.

## Needs Lake

- **N1. Rule D.** Confirm rule (D) as part of the contract. It formalises Stop-to-skip and exactly-once durable effects, which the reviews have enforced since round 7.
- **N2. Old body fence (L12, R9-CX-F09).**
  - **Proposed:** a close or takeover does not give the lease away while the old turn body can still act.
    - A close keeps the lease as a retained owner until the body exits, as the isolated-turn path already does, with the waiting notice shown.
    - A takeover's new turns wait for the old body the same way they already wait for its Stop.
  - **Remaining behaviour if not accepted:** after the close grace, and immediately on takeover, a still-running old body can overlap the new owner's preparation and callbacks. The DB turn lease still prevents concurrent transcript turns.
- **N3. Busy-interrupt launch failure (X6, R9-CX-F21).**
  - **Proposed:** undo exactly that queue entry and return the failure, so the client can retry without duplicating the work.
  - **Alternative:** keep the prompt queued and return an honest "queued" result with an interruption diagnostic.
- **N4. Approval namespace (O8, R9-CX-F08).**
  - **Proposed:** key approval queues and callbacks by (profile home, stored id) in `tools/approval.py`.
  - **Scope note:** this touches a module shared with the messaging gateway. The gateway's single-profile callers keep today's behaviour.
- **N5. A Stop sent to a respawned host without the session (HB7).**
  - **Proposed:** when the host acks `applied=False` for a turn the parent still owns, the parent revokes that turn's claim if the turn has not started on a host. Otherwise it ends the turn as cancelled with a client-visible error.
  - **Alternative:** report "Stop not applied" to the client and leave the turn running.

## Part 3: disposition of every A25 finding (rounds 1–9)

**Outcomes:**
- **FIXED:** the named test passes at the named commit and at `9f133723bd`.
- **OPEN:** the named Part 2 row owns it.
- **DISPROVED:** with evidence.
- **LAKE:** a decision from "Needs Lake" is attached.

Antigravity entries that repeat a same-round Codex finding with the same probe take the Codex entry's outcome ("AG = CX"). Antigravity filed no report for rounds 1, 2 and 8 (round 8 was started and abandoned), and approved rounds 3, 4 and 9 with no findings. Sources are the `REVIEW.md` files under `review-artifacts/a25-*`.

| finding | summary | outcome |
|---|---|---|
| R1-CX-1 | a compressed loop wake-up settles the old session | FIXED @63bd2e97cc: `test_a_loop_wakeup_whose_turn_compresses_settles_the_continuations_loop[complete|stopped|failed]` |
| R1-CX-2 | after the wait expires, the old busy interrupt hits the successor | FIXED @ef77d8ac87 (there is no expiry under Lake's no-deadline wait): `test_an_earlier_turns_busy_interrupt_never_escapes_to_the_next_turn[wait-expires]` |
| R1-CX-3 | a second busy interrupt for the new turn is coalesced away | FIXED @63bd2e97cc: `test_an_earlier_turns_busy_interrupt_never_escapes_to_the_next_turn[second-busy-input]` |
| R1-CX-4 | an old drain's refusal clears a newer turn's running state | FIXED @ef77d8ac87: `test_a_refused_drain_admission_never_releases_the_turn_that_replaced_it` |
| R1-CX-5 (A19b) | a failed git worker start probes inline | FIXED @b05b4b2f79: `test_turn_end_branch_probe.py::test_a_refresh_that_cannot_start_its_thread_never_probes_on_the_caller` |
| R2-CX-1 | an old interrupt still cancels the successor's request, tool thread and child | FIXED @ef77d8ac87: `test_an_earlier_turns_busy_interrupt_never_cancels_the_next_turns_work` |
| R2-CX-2 | an old drain's refusal removes a replacement drain's claim | FIXED @ef77d8ac87: `test_an_old_drains_refusal_never_releases_a_newer_drains_claim[owner|host|generation]` |
| R3-CX-F1 | a deferred busy interrupt unregisters before its propagation ends | FIXED @c4cad7957b: `test_a_deferred_busy_interrupt_still_propagating_never_reaches_the_turn_after_a_stop` |
| R3-CX-F2 | the backend ready frame does not clear the desktop waiting notice | FIXED @1192149410: `apps/desktop/.../status-notice-event.test.tsx` ("shows a waiting notice…", "leaves a provider wait shown…") |
| R4-CX-F1 | an explicit Stop has no propagation owner visible to admission | FIXED @5d1d8d85a0: `test_a_cancellation_still_propagating_never_touches_the_next_turns_work[stop]` |
| R5-CX-F1 / R5-AG-F1 | a voice barge-in for the waiting turn is cleared as stale | FIXED @b1727b9aca: `test_an_interrupt_aimed_at_the_waiting_turn_survives_the_older_cancellation_settling[voice-barge-in]` (AG = CX) |
| R5-CX-F2 / R5-AG-F2 | a second submit uses a transferred lease before the takeover Stop settles | FIXED @b1727b9aca: `test_a_second_submit_cannot_use_a_taken_over_lease_before_the_old_runtimes_stop_settles` (AG = CX) |
| R6-CX-F1 / R6-AG-F1 | in-flight and inherited cancellations do not follow a transferred lease | FIXED @3b772a2cd5: `test_a_takeover_inherits_every_cancellation_still_using_the_stored_session[existing-stop|two-hops]` (AG = CX) |
| R7-CX-F1 / AG-F1 | a revoked ordinary submit starts inside its successor | FIXED @b034536955: `test_revoked_ordinary_submit_cannot_enter_successors_turn` (the residual schedules became R8-CX-F1, F2, F3, F8) |
| R7-CX-F2 / AG-F2 | redirect is an unregistered cancellation | FIXED @b034536955: `test_redirect_propagation_cannot_abort_successors_request[busy-redirect|session-redirect]` |
| R7-CX-F3 / AG-F3 | the host soft interrupt bypasses the registry | FIXED @b034536955: `test_compute_host_soft_interrupt_cannot_propagate_into_queued_successor` |
| R7-CX-F4 / AG-F4 | close forgets an outstanding cancellation | FIXED @b034536955: `test_close_keeps_outstanding_cancellation_owned_until_it_settles` |
| R7-CX-F5 / AG-F5 | a never-owning runtime stops the owner's delegations | FIXED @b034536955: `test_runtime_that_never_owned_lease_cannot_stop_owners_delegations` |
| R7-CX-F6 / AG-F6 | a revoked drain's exception releases the successor | FIXED @b034536955: `test_exception_from_revoked_drain_cannot_release_successor` |
| R7-CX-F7 / AG-F7 | a retired runtime's Stop denies the successor's approvals | FIXED @b034536955: `test_retired_runtime_stop_cannot_deny_successors_approval` (the cross-profile case is R9-CX-F08, O8) |
| R7-CX-F8 / AG-F8 | Stop-to-skip enters input preparation | FIXED @b034536955: `test_stop_to_skip_cancellation_wait_never_enters_input_preparation` |
| R8-CX-F1 | a drain revoked after admission starts beside its successor | FIXED @9f133723bd: `test_drain_revoked_after_admission_cannot_start_after_successor` |
| R8-CX-F2 | host dispatch drops the acceptance claim | FIXED @9f133723bd: `test_a_revoked_submit_is_not_dispatched_to_the_compute_host`, `test_host_completion_settles_only_the_claim_it_was_dispatched_under` |
| R8-CX-F3 | a revoked branch submit re-appends the seed | FIXED @9f133723bd: `test_a_revoked_branch_submit_cannot_append_its_seed_after_the_successor` (the fix's retry regression is R9-CX-F01, E1) |
| R8-CX-F4 | an intermediate owner's close forgets inherited cancellations | FIXED @9f133723bd: `test_inherited_cancellation_is_not_lost_when_the_intermediate_owner_closes` |
| R8-CX-F5 | close uses ownership from before releasing the lease | OPEN (O2, O6, L3). Fixed for manual close @9f133723bd (`test_close_does_not_use_ownership_from_before_releasing_the_lease[delegation|approval]`); the same property still fails on the automatic path and after held-lease release (R9-CX-F05, F06) |
| R8-CX-F6 | cancellation target selection is separate from registration | FIXED @9f133723bd: `test_correction_selected_for_old_turn_does_not_hit_the_successor[redirect|voice]`, `test_hosted_task_stop_remains_bound_to_the_checked_task` (the intent leak of a refused hosted Stop is R9-CX-F17, X3) |
| R8-CX-F7 | an old Stop revokes the new waiting claim | FIXED @9f133723bd: `test_old_stop_cannot_revoke_the_claim_that_started_waiting_for_it` (the flag/generation binding is R9-CX-F04, X1) |
| R8-CX-F8 | reset publishes over a newly admitted turn | OPEN (X14). Its probe passes @9f133723bd (`test_agent_reset_cannot_publish_over_a_turn_admitted_while_rebuilding`), but the agent replacement it describes still happens (R9-CX-F03) |
| R8-CX-F9 | the delegation selector crosses profiles | FIXED @9f133723bd: `test_owner_stop_is_scoped_to_its_profile_not_a_copied_session_id` (approvals are R9-CX-F08, O8) |
| R8-CX-F10 | a late agent-ready error fails the successor's record | FIXED @9f133723bd: `test_late_agent_ready_failure_does_not_fail_the_successors_inflight_record`, `test_a_missing_agent_refusal_fails_only_its_own_turn` (the publication gap is R9-CX-F14, P9) |
| R8-CX-F11 | a never-owning viewer ends the owner's row | OPEN (O7). Fixed for an owner in this backend @9f133723bd (`test_viewer_close_cannot_end_the_current_owners_database_row`); still fails for an owner outside this backend (R9-CX-F10) |
| R8-CX-F12 | a Stop at the final wait completion enters preparation | FIXED @9f133723bd: `test_stop_at_the_last_cancellation_completion_still_skips_input_preparation` |
| R8-CX-F13 | the Stop RPC tail deletes the successor's crash marker | FIXED @9f133723bd: `test_stop_rpc_tail_cannot_remove_a_successors_crash_marker` (the turn-tail variant is R9-CX-F16, P10) |
| R8-CX-F14 | a failed worker start strands the claim | FIXED @9f133723bd: `test_submit_worker_start_failure_releases_its_claim` |
| R8-CX-F15 | the census omits post-admission boundaries | OPEN (2.8), together with R9-CX-F22 |
| R8-CX note (unnumbered) | Stop waits on the user-row write under `history_lock` | FIXED @9f133723bd: the write no longer holds `history_lock` (the claim is checked inside the transaction); `test_a_revoked_submit_never_leaves_its_user_row_in_the_stored_transcript[successor-writes-first|revoked-resumes-first]` still passes |
| R9-CX-F01 | a retried seed transaction publishes success with zero rows | OPEN (E1) |
| R9-CX-F02 | a partly committed seed duplicates on retry | OPEN (E2) |
| R9-CX-F03 | reset replaces another turn's agent, erases its state, and releases a started turn | OPEN (X14, L10) |
| R9-CX-F04 | an old Stop changes the successor's cancellation state; an idle Stop's legacy tail | OPEN (X1, L8) |
| R9-CX-F05 | automatic close releases the lease before cleanup | OPEN (O2, O6) |
| R9-CX-F06 | cancellation completion releases a closing lease that finalize still uses | OPEN (L3, O2) |
| R9-CX-F07 | the late `agent.close()` re-ends a reopened row | OPEN (O5, E8) |
| R9-CX-F08 | approval cleanup crosses profiles | OPEN (O3, O4, O8, O9, P16); LAKE N4 for the key shape |
| R9-CX-F09 | close and takeover fence propagation but not the old body | OPEN (L12, X5, P14); LAKE N2 |
| R9-CX-F10 | a viewer close checks only this backend | OPEN (O7) |
| R9-CX-F11 | an adopted cancellation finishes before holder registration | OPEN (L4) |
| R9-CX-F12 | busy-worker completion bypasses held-lease settlement | OPEN (X6, L4) |
| R9-CX-F13 | a failed deferred release drops its owner | OPEN (E5) |
| R9-CX-F14 | terminal-error publication has a gap after its claim check | OPEN (P8, P9, E6) |
| R9-CX-F15 | revoked or refused paths emit unscoped terminal events | OPEN (P1, P9, E6) |
| R9-CX-F16 | the post-release tail removes the successor's marker | OPEN (P8, P10, E3) |
| R9-CX-F17 | a refused hosted Stop publishes interrupt intent | OPEN (X3) |
| R9-CX-F18 | host busy input loses the next host turn's interrupt | OPEN (X7) |
| R9-CX-F19 | a Stop before a loop body starts leaves the loop active | OPEN (T8, T10, P11) |
| R9-CX-F20 | refused notification turns are acknowledged as delivered | OPEN (T11, T12, T13, P12, E7) |
| R9-CX-F21 | a busy-interrupt launch failure leaves accepted work queued | OPEN (X6); LAKE N3 |
| R9-CX-F22 | the census does not cover the boundaries it claims | OPEN (2.8) |
| A28-H9 | control head-of-line blocking on the host | OPEN (HB4) |
| A28-I1 | a Stop overtakes a queued `turn.start`; an old `turn_id`; a respawned host | OPEN (HB5, HB6, HB7, X15); LAKE N5 |

## Counts

- **Inventory rows: 92** (91 table rows — T 17, X 18, P 18, O 9, L 12, HB 8, E 9 — plus the census in 2.8). 3 are excluded (T17, X17, X18), leaving **89 in scope**:
  - **done: 24**
  - **partly done** (a named test guards part; a named OPEN part remains): **26**
  - **OPEN: 35**
  - **BASELINE** (pre-A25, no A25 finding): **4** (P7, L1, L6, L7)
- **Findings: 58 unique Codex findings** over rounds 1–9, plus 1 unnumbered R8 note. By round: R1 5, R2 2, R3 2, R4 1, R5 2, R6 1, R7 8, R8 15, R9 22.
  - **11 Antigravity entries**, all same-round duplicates of Codex (R5 2, R6 1, R7 8).
  - Plus **A28-H9** and **A28-I1**.
- **Outcomes:**
  - **FIXED: 32 findings plus the R8 note.** By round: R1 5, R2 2, R3 2, R4 1, R5 2, R6 1, R7 8, R8 11 (F1, F2, F3, F4, F6, F7, F9, F10, F12, F13, F14; F5, F8, F11 and F15 are open). The Antigravity duplicates follow their Codex entries.
  - **OPEN: 26 findings plus H9 and I1 = 28.** These are R8-CX-F5, F8, F11, F15 and all 22 of R9.
  - **DISPROVED: 0.**
  - **LAKE:** 4 decisions attached to OPEN rows (N2–N5), plus N1 on the contract.
