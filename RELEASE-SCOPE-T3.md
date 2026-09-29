# T3 release scope: the desktop runtime-identity contract, its path inventory, and the disposition of every T3 finding

**DRAFT for Lake (not frozen).** Drafted 2026-09-28 at `magi/track-desktop` `44e26582a2`, while round 29 is under review. Model: `RELEASE-SCOPE-A28.md`. The sources are:
- `.t3-fix-r24-20260928/PATHS.md` (per-round finding→fix→test tables);
- the review reports `.t3-review-r10…r28/`;
- `.t3-residuals-20260927/DISPOSITIONS.md`;
- the track's commit messages, `242bad5ab4..44e26582a2` (58 commits).

All paths are in the worktree `E:\GitHub\Magi-AGI\hermes-worktrees\desktop`. Source references are relative to `apps/desktop/` and are line numbers at `44e26582a2`.

## Baseline and rules

- **Code baseline.** `magi/track-desktop` at `44e26582a2`, a clean tree. The track range is `242bad5ab4..44e26582a2`. The branch is not merged into `magi/sync-v2026.9.21`.
- **What the T3 release is.** Fixes to existing code in the desktop renderer's session identity, resume and completion paths. No new general framework is added: the work list (Part 2's OPEN rows) closes gaps in the boundary that already exists. That boundary is:
  - the authority `src/app/session/runtime-bindings.ts`;
  - the registry and clock `src/store/runtime-identity.ts`;
  - the owner keys `src/app/session/owner-key.ts`;
  - the guarded cache `src/app/session/session-state-cache.ts`.
- **What "fixed" means.** A row is *done* only when both are named:
  - a behavioural test that passes at the stated commit;
  - the commit that made it pass.
  
  For every row marked done, the test passed at `44e26582a2` in the full `ui` project run (992 files, 8,868 tests). The log is `.t3-fix-r29-20260928/ui-final2.log`.
- **Combined-release validation is a separate gate.** This list proves T3 on its own branch. Merging with the sync branch and the other tracks needs its own run, which is not claimed here: `DEPLOY-GATE-CHECKLIST.md`.
- **Test commands.** Run from `apps/desktop` with the pinned Node on `PATH` (`E:\GitHub\Magi-AGI\hermes-agent-sync\.tools\node-v26.10.0-win-x64`):
  - `node ../../node_modules/vitest/vitest.mjs run --project ui <files>`;
  - the census: `--project electron scripts/runtime-identity-census.test.ts`;
  - `tsc -p tsconfig.json`, `tsconfig.electron.json` and `tsconfig.e2e.json`;
  - `eslint src/ electron/ scripts/runtime-identity-census.test.ts`.
- **Reviewer corpora (not repo tests).** They live under `.t3-review-r26…r28/`. At `44e26582a2`:
  - r28: 28 of 36 pass. The 8 that fail are the lint-only census forms (see P8).
  - r27: 29 of 29 pass.
  - prior: 82 of 83 pass. The 1 is the r26 orphan-close expectation, which was reversed on purpose; Codex accepted the reversal in r28.
  - census: 42 of 42 pass.

## Part 1: the contract (behaviour properties)

Each property is written as behaviour a test can observe. Part 2 maps every path to the properties it must satisfy.

### P1: One owner-qualified identity authority

- **The property.** Which runtime serves which stored session, and under which owner, is decided in exactly one place:
  - **Reads:** `runtimeFor`, `validatedRuntimeFor`, `storedIdFor`, `ownerOf`, `exactOwnerOf`.
  - **Writes:** `adopt`, `acceptResumeReply`, `ensure`, `forget`, `retainOnly`, reclaim.
  
  All of these belong to `RuntimeBindings`, whose implementation is `BindingsCore` at `runtime-bindings.ts:204`.
- **Owner-keyed bindings.** A binding is keyed by (owner key, stored id). Two owners holding the same durable id never share, overwrite or drop each other's binding.
- **Identity moves forward only.** A runtime never moves back to a stored id it has left. A refused adoption changes nothing: neither the recorded owner nor any binding.
- **The cache's identity field belongs to the authority.** A content write (`SessionStateCache.set`, `session-state-cache.ts:166`) cannot introduce or change an identity. The cache's `guard` (`:135`) takes the identity from the registry, not from a mutable row.
- **Gateway switches.** Every read and write runs a synchronous gateway-epoch check first. After a switch, no reader sees a runtime the switch retired.

### P2: One comparison per completion

Every completion that arrives after an await makes one comparison before it writes, publishes, dispatches, notifies or cleans up. This covers a reply, an error, a rollback, a notice and a cleanup. The comparison is `ticketHolds(ticket, {sameTurn?, exclusive?})` at `runtime-identity.ts:217`, where the ticket comes from `beginOperation(runtime?)` at `:201` when the operation starts. A resume reply is judged by its flight's dispatch stamp instead (`acceptResumeReply`), which is the same comparison. Every stamp uses one monotonic clock. The ticket fails when any of these happened since the operation began:
- **Gateway:** a gateway switch.
- **Runtime:** the backend reclaimed the ticket's runtime (`session.reclaimed`).
- **Turn, only for completions that overwrite runtime state:**
  - `sameTurn`: a new turn started on the runtime (a new turn-start clock, or a backend turn id it had not run before).
  - `exclusive`: any content write reached the runtime.

A transport reconnect fences in-flight replies. A reply refused only because of the fence is retried once and logged. A gateway switch is never retried.

### P3: Owner keys are canonical and round-trippable

- **The form.** The key is `<connection>|<profile>[|<targetProfile>]`, built by `ownerKeyOf` (`owner-key.ts:56`):
  - `profile` is the socket-selecting Desktop profile;
  - a `targetProfile` that differs from it is kept;
  - a bare profile, or no owner, is the ambient route (the window's own connection).
- **Round-trip.** `ownerScopeOfKey` (`:76`) returns the route that dispatches to the same socket.
- **One key everywhere.** Bindings, resume flights, recovery hints, submit claims and journal slots all use this same key.
- **Callers without an owner** use one defined rule, `ownerKeyForStoredSession` (`:103`):
  1. the durable owner record;
  2. else the owner record of a continuation's single lineage;
  3. else the ambient route.
  
  The rule never searches existing bindings.

### P4: Owner-token parity with backend A25

Every release or ownership decision compares a claim; nothing is released or taken on a stale holder's behalf:
- **The submit lock** is a claim with a token (`claimSubmitInFlight`, `utils.ts:478`). It is keyed by owner and stored id. A release frees only its own claim (`:503`).
- **A shared resume flight** is joined only by the same owner (`singleFlightSessionResumeStamped`, `single-flight-resume.ts:58`). The flight removes only its own entry.
- **A runtime's recorded owner** changes only on an accepted adoption.
- **Primary and tile rewinds, a send's settlement and a Stop's tail** settle only the turn they started.

### P5: Per-owner persisted state

- **The in-flight turn journal** has one slot per owner and stored id (`journalSlot`, `inflight-turn-journal.ts:102`):
  - It is recovered only under the gateway epoch it was written in.
  - An entry without an epoch (written by an older build) is trusted only until the first recorded gateway switch.
  - A settle clears only the turn it settles.
- **Other persisted identity-bearing state** must meet the same property: owner-scoped, epoch-safe, and never read back under another owner. Part 2's H and F6 rows cover each store:
  - transcript tail cache;
  - handoff receipts;
  - owner hints;
  - read-only latch;
  - the gateway-epoch id itself.

### P6: First-dispatch stamping

- **The rule.** An operation is stamped at entry, before its first await. The stamp is checked immediately before each side-effecting request that follows a preparatory await: a file read, a profile lookup, a history read, a handshake, personalization or a seed build.
- **Why at dispatch.** A rejected reply cannot undo a request that was already sent: an upload, a create, a truncation, a prompt.
- **The enforcement point.** `withSessionNotFoundResume` refuses its first call when the enclosing stamp predates a switch (`OperationRetiredError`, `utils.ts:86`).

### P7: The runtime owner guard

- **The runtime guard.** `runtimeFor` and `validatedRuntimeFor` throw `TypeError` without an owner, however the call was written (`requireOwner`, `runtime-bindings.ts:196`).
- **TypeScript.** It requires the owner for typed callers of these two methods and of `acceptResumeReply`.
- **Acceptance.** An untyped `acceptResumeReply` without an owner binds under the runtime's recorded owner or the defined rule. It never answers with another owner's runtime.

### P8: The census and its documented limits

**What it is.** The ESLint census (`eslint.config.mjs:45-145`, applied at `:217-218`) keeps production modules outside the authority from importing:
- the registry mutators;
- the cache's privileged identity write;
- the recovery-hint cache;
- the test seams;
- the concrete authority class;
- test modules.

It also refuses these syntactic forms:
- the raw map name;
- borrowed Map mutators;
- a literal dynamic import of authority or test modules;
- owner-less calls, by argument count.

`scripts/runtime-identity-census.test.ts` proves each case against the real configuration, and proves that the plugin fence and the ref-mirror rule still apply.

**Qualified completeness claim.** The census and the runtime guard cover only what they recognise. They are evidence, not proof, that the inventory is complete. Known blind spots:
- **Dynamic-import forms.** A `import()` of a variable, `import.meta.glob`, and `require` are a documented limit, not linted. A variable import has no static specifier; `import.meta.glob` is a Vite build-time form, and no production code uses it on authority modules; `require` does not exist in the ESM renderer at run time.
- **Receivers typed as `any`** escape the TypeScript check, though not the runtime guard.
- **Stores the census does not name.** It cannot see identity-bearing state written outside the authority: `publishSessionState` or `$sessionStates`, localStorage caches, tile routes, owner hints.
- **Semantic ownership.** A lint rule cannot prove that an asynchronous completion still owns its target. Only the behavioural tests in Part 2 do.

**Invited challenge.** An independent source audit is expected to challenge Part 2's completeness. The r29 review prompts ask for one. A path it finds becomes a new Part 2 row, classified by Part 3's outcomes.

### P9: The whole effect lifetime

The properties apply to an operation's entire lifetime, not only to its request and reply:
- **Persisted state:** the journal, tail cache, receipts, owner hints and latch.
- **Late notifications and toasts:** these may inform, but never write another owner's or turn's state.
- **Cleanup after a gateway switch:** no ambient `session.close` for a runtime minted by the retired gateway; the retired gateway's own orphan reap owns that runtime. Codex accepted this in r28.
- **Failed or duplicate closes and releases:** a failed release neither frees another's claim nor leaves a claim held forever. The submit claim expires after 30 s.


## Part 2: the inventory (every identity-carrying path and late completion at `44e26582a2`)

### How to read this inventory

- **Method.** The inventory was built by a source search of the whole renderer. The search terms were:
  - request methods: `session.resume`, `session.activate`, `session.create`, `session.branch`, `session.close`, `prompt.submit`, `session.interrupt`, `session.compress`;
  - authority calls: every `adopt`, `acceptResumeReply`, `runtimeFor`, `updateSessionState`, `ensureSessionState`;
  - writers and triggers: `notify` calls, persisted-state writers, `advanceRuntimeFence` and reconnect hooks.
  
  Every line number was re-read at `44e26582a2`.
- **What "done" means.** A row is *done* only when a test at `44e26582a2` drives **that path's** property. A test that exercises only the underlying mechanism does not count. The table uses these abbreviations:
  - `T=` names the test;
  - `F=` names the commit that made it pass, as a short hash (`r27` = `1e8a6aa20b`, `r28` = `fe4b930d99`, `r28b` = `4ba5f4131e`, `r29` = `44e26582a2`, `r25` = `6a483bc60f`).
- **The Property column** names P1–P9 from Part 1.
- **Test file paths** are relative to `apps/desktop/src/`.

**Totals: 97 rows: 48 done, 49 OPEN.**

The OPEN rows are the concrete work list (see "Work list"). They close gaps in the existing boundary: each needs the existing ticket, stamp or owner applied at one more site, plus a behavioural test. None needs a new mechanism. The exceptions are rows G7, G12, H3–H6 and J2–J3, which need a decision on scope; they are listed under "Needs Lake".

### A. Resume

| # | Path (file:line) | Property the path must satisfy | P | Test / status |
|---|---|---|---|---|
| A1 | Primary cold resume flight and acceptance, `app/session/hooks/use-session-actions/index.ts:1832-1972` | The reply binds under the owner the resume was routed to, and names the continuation. Transcript selection uses the accepted id | P1 P2 P3 | done. T=`app/session/gateway-epoch.test.ts:271`, `resume-ordering.test.ts:224`, `use-session-actions/pre-dispatch.test.ts:138`. F=r25/r28 |
| A2 | Primary resume running flag, `index.ts:2071` | An older snapshot never revives a turn that ended | P2 | done. T=`pre-dispatch.test.ts:167`. F=r28 |
| A3 | Primary cold resume, first dispatch, `index.ts:1214-1255` → `:1832` | A switch during the owner/gateway preparation sends nothing. The stamp is taken at entry, not after `resolveStoredSession` / `ensureGateway*` (today it is taken at the flight, and `isCurrentResume` uses render-synced refs). The `/resume` slash command and the routed submit inherit this | P6 | **OPEN** |
| A4 | Primary resume failure path, `index.ts:2244-2412` | The REST fallback paint, the read-only latch (`:2324`), toasts (`:2325`, `:2405`), the failure latch and the busy flags land only while the operation's ticket holds | P2 P9 | **OPEN** |
| A5 | Journal recovery on resume, `index.ts:2083`, `:2272` | Recovers only the resume owner's journal | P3 P5 | done. T=`lib/inflight-turn-journal-owner.test.ts:35`, `inflight-turn-journal-scope.test.ts:18`. F=r28/r29 |
| A6 | Warm lookup `takeWarmCache`, `index.ts:1155-1170` | Owner-keyed lookup; forgets only its own runtime | P1 P3 P7 | done. T=`gateway-epoch.test.ts:191`. F=r25/r28b |
| A7 | Warm activation, `index.ts:1389-1500` | A fenced reply is not applied; a newer write wins over the reply's running flag | P2 | done. T=`use-session-actions/gateway-epoch-completions.test.tsx:98`, `warm-activation-age.test.ts:42`. F=r25/r29 |
| A8 | Warm activation's older-backend branch (`session.usage`), `index.ts:1405-1416` | The usage/degraded-cache writes hold the ticket | P2 | **OPEN** |
| A9 | Warm activation REST merge and tail save, `index.ts:1571-1716` | Lands only while `owns()` (which carries a ticket) holds. There is no test at this site | P2 P5 | **OPEN** (the mechanism is tested in `authority-boundary-r28.test.ts:95`, the site is not) |
| A10 | Tile cold resume acceptance, `app/contrib/hooks/use-session-tile-delegate.ts:449-523` | Owner-keyed acceptance by stamp; the running flag needs `exclusive` | P1 P2 P3 | done. T=`gateway-epoch.test.ts:244`, `resume-ordering.test.ts:166`, `runtime-lineage.test.ts:124`, `contrib/hooks/tile-continuation.test.ts:38`. F=r25/r29 |
| A11 | Tile cold resume, first dispatch, `use-session-tile-delegate.ts:319` → `:372` | A switch during the `ownerForStoredSession` probe await sends no `session.resume` | P6 | **OPEN** |
| A12 | Tile warm path and REST merge, `:303-352` | Retired transports are handed back; the merge is content only | P1 P2 | done. T=`gateway-epoch.test.ts:209`, `runtime-boundary.test.ts:150`. F=r25/r27 |
| A13 | Tile read-only fallback, `:359-431` | The latch and notice appear only while the operation is current | P2 P5 P9 | done. T=`runtime-boundary.test.ts:123`. F=r27 |
| A14 | Target resolver, `app/session/hooks/use-prompt-actions/resolve-target-session.ts:87-185` | Stamped at entry; nothing sent or adopted after a switch; adopts under its owner | P2 P3 P6 | done. T=`gateway-epoch.test.ts:125`, `pre-dispatch-r29.test.ts:47`. F=r25/r29 |
| A15 | Submit direct resume acceptance, `use-prompt-actions/submit.ts:855-876` | Owner-keyed acceptance by stamp | P1 P2 P3 | done. T=`submit-gateway-epoch.test.ts:15`, `recovery-boundary.test.ts:56`. F=r25/r28 |
| A16 | Submit direct resume, first dispatch, `submit.ts:801-821` | A switch during `resolveSessionProfile` sends no `session.resume` for the retired send. Today only the hint is gated (verified at `:801-803`) | P6 P2 | **OPEN** |
| A17 | Submit routed resume, `submit.ts:727-783` | Inherits A3; the drift hint is registered after an async lookup | P2 P3 | **OPEN** |
| A18 | Recovery helper `withSessionNotFoundResume`, `use-prompt-actions/utils.ts:200-318` | Stamped at entry; no recovery, hint retry or replay after a switch; hints owner-keyed | P2 P3 P6 | done. T=`recovery-boundary.test.ts:35`, `:56`, `gateway-epoch.test.ts:143`, `single-flight-resume.test.ts:70`, `:101`. F=r28/r29 |
| A19 | Recovery adoptions in every `onRecovered` without evidence: `use-prompt-actions/index.ts:385`, `:792`, `:1008`; `slash.ts:579`, `:719`; `steering-session.ts:84`; `submit.ts:640`; `use-session-tile-delegate.ts:170`, `:560` | The recovered binding lands under the same owner as the recovery flight and hint, with the flight's stamp | P3 P4 | **OPEN** |
| A20 | Onboarding resume `handoff-leg.ts:83-107` | Routed to the receipt owner; binds the reply's continuation | P2 P3 | done. T=`contrib/onboarding-handoff-owner.test.ts:92`, `onboarding-handoff-epoch.test.ts:38`, `onboarding-handoff-ownership.test.ts:59`. F=r25/r29 |
| A21 | Handoff receipt saved before the transport check, `handoff-leg.ts:106` then `:107` | A receipt is persisted only for a snapshot that passes the fence | P2 P5 | **OPEN** |
| A22 | Route resume, `app/session/hooks/use-route-resume.ts:147` | Owner-keyed lookup; no resume for a retired gateway | P1 P3 | **OPEN** (the suite has no owner or switch case) |
| A23 | Background queue drain, `use-background-queue-drain.ts:122-140`, toast `:101` | The post-submit cleanup and the "queue stuck" toast hold the ticket; the owner is tested | P2 P3 P9 | **OPEN** |
| A24 | Shared resume flights `single-flight-resume.ts:58-97` | Owner-keyed; stamped at dispatch; a fenced flight is not joined | P2 P3 | done. T=`resume-ordering.test.ts:118`, `runtime-lineage.test.ts:261`, `ownership-boundary-r29.test.ts:73`. F=r23/r28/r29 |

### B. Create, branch, close, locks

| # | Path | Property | P | Test / status |
|---|---|---|---|---|
| B1 | `createBackendSessionForSend` pre-dispatch, `index.ts:586`, `:646-652` | Nothing sent after a switch during preparation | P6 | done. T=`pre-dispatch.test.ts:105`. F=r28 |
| B2 | Create reply after a switch or reconnect, `index.ts:700-750` | Binds nothing; no close on the next gateway's ambient socket; a fenced runtime is refused | P2 P9 | done. T=`gateway-epoch-completions.test.tsx:139`, `:163`. F=r27/r28 |
| B3 | Create owner hint, `index.ts:683` (tile create: `:968`) | The persisted owner hint and hold are written only for a create that survives the switch/drift decision | P5 | **OPEN** |
| B4 | Post-create YOLO, `index.ts:795` | Sent to the route that minted the runtime, not the ambient socket | P3 | **OPEN** |
| B5 | New-tile create `openNewSessionTile`, `index.ts:860-1060` | The same pre-dispatch, switch and fence behaviour as B1/B2, and the `notifyError` (`:1059`) holds the ticket | P2 P6 P9 | **OPEN** (untested) |
| B6 | `forkBranch`, `index.ts:2449-2659` | Checked after the owner handshake; a fenced flight is not joined; returns false after a switch | P2 P4 P6 | done. T=`pre-dispatch.test.ts:83`, `gateway-epoch-completions.test.tsx:181`, `use-session-actions.test.tsx:345`. F=r25b/r28 |
| B7 | Branch entry stamps, `index.ts:2694`, `:2791` | Stamped before the parent transcript read | P6 | done. T=`gateway-epoch-completions.test.tsx:181`. F=r27 |
| B8 | Branch failure toasts, `index.ts:2670`, `:2840` | Shown only while the ticket holds | P9 | **OPEN** |
| B9 | Delete session, `index.ts:2866-2990` | The close goes to the removed session's owner. The forget uses that owner's key, not the rule. The failure rollback never restores a retired runtime | P2 P3 P9 | **OPEN** |
| B10 | Archive, `index.ts:3007-3062` | As B9 | P2 P3 P9 | **OPEN** |
| B11 | Submit lock, `submit.ts:412-431`; `utils.ts:478-523` | Owner-qualified claim; only the holder's token releases it | P3 P4 | done. T=`recovery-boundary.test.ts:68`, `submit-lifetime.test.ts:79`. F=r28/r29 |
| B12 | Flight cleanup (single-flight, `branchCreateFlightsRef`) | Deletes only its own entry | P4 | done. T=`single-flight-resume.test.ts:57`, `use-session-actions.test.tsx:345`. F=r25b |

### C. Submit and Stop

| # | Path | Property | P | Test / status |
|---|---|---|---|---|
| C1 | Seed and late writes, `submit.ts:436-449`, `:516-576` | No write after a switch or after its runtime is reclaimed | P2 | done. T=`submit-gateway-epoch.test.ts:66`, `submit-lifetime.test.ts:19`. F=r25/r29 |
| C2 | Settlement, `submit.ts:458`, `:497-511`, `:1145-1167` | Settles only the send's own turn | P2 P4 | done. T=`submit-lifetime.test.ts:42`, `turn-generation.test.ts:64`. F=r29 |
| C3 | Recovery handoff `handOff`, `submit.ts:632-649` | Not after a switch; seeds the recovered runtime | P2 | done. T=`submit-handoff.test.ts:14`, `:43`, `submit-late-ack.test.ts:127`. F=r18/r19 |
| C4 | Attachment sync inside submit, `submit.ts:972` → `use-prompt-actions/index.ts:429` | The upload and its recovery carry the SEND's stamp: a switch during submit's earlier resume or create sends no `file.attach` | P6 | **OPEN** |
| C5 | `prompt.submit` dispatch, `submit.ts:1038-1060` | Carries the send's stamp; busy retries stop after a switch | P2 P6 | done. T=`submit-gateway-epoch.test.ts:15`. F=r28 |
| C6 | Ack `user_row_id` stamp, `submit.ts:1097-1112` | Behind the send's ticket | P2 | done. T=`submit-late-ack.test.ts:101`. F=r17/r29 |
| C7 | Failure card, `submit.ts:1145-1168` | Card on the operation's runtime; no settle of a newer turn | P2 | done. T=`submit-handoff.test.ts:43`, `:106`, `:196`, `submit-lifetime.test.ts:19`. F=r19/r29 |
| C8 | Submit failure and create toasts, `submit.ts:905`, `:929`, `:1170-1186` | Shown only while the send's ticket holds (today: view drift only) | P9 | **OPEN** |
| C9 | Foreground `releaseBusy` / `dropOptimistic`, `submit.ts:497-511`, `:590-617` | Only for the send's own turn | P2 | done. T=`submit-ownership.test.ts:215`. F=r20/r29 |
| C10 | Primary Stop tail, `use-prompt-actions/index.ts:774-798` | Releases foreground busy only for its own turn | P2 | done. T=`submit-lifetime.test.ts:121`. F=r29 |
| C11 | Primary Stop failure toast, `index.ts:801` | Ticketed | P9 | **OPEN** |
| C12 | Tile submit (shared `useSubmitPrompt`), `app/chat/session-tile-actions.ts:338-366` | As C1–C9 | P2 | done. T=`app/chat/session-tile-recovery.test.ts:33`, `session-tile-attachment-recovery.test.ts:61`. F=r18/r19 |
| C13 | Tile attachment upload, `session-tile-actions.ts:273-330` | The recovery flight and hint use the tile's composite owner key (today: the profile key) | P3 | **OPEN** |
| C14 | Tile `cancelRun`, `session-tile-actions.ts:392-429` | Toast ticketed | P9 | **OPEN** |
| C15 | Delegate `submitToSession` / `interruptSession`, `use-session-tile-delegate.ts:240-268`, `:530-557` | The first-dispatch check runs after the owner-probe await inside `routedRequest` | P6 | **OPEN** |

### D. Slash commands

| # | Path | Property | P | Test / status |
|---|---|---|---|---|
| D1 | `withSlashOutput` ticket, `slash.ts:242-300` | One ticket per command, reused by the fallback; names its runtime | P2 | done. T=`late-consumers.test.ts:43`. F=r28/r29 |
| D2 | `/compress`, `slash.ts:676-840` | Transcript replacement only in the same turn; nothing after a reclaim or switch | P2 | done. T=`turn-generation.test.ts:20`. F=r29 |
| D3 | `/stop`, `slash.ts:559-610` | `process.stop` is not sent after a switch | P6 | **OPEN** |
| D4 | Slash create-failure toast, `slash.ts:263` | Ticketed | P9 | **OPEN** |
| D5 | `/yolo` failure toast, `slash.ts:903-925` | Ticketed | P9 | **OPEN** |
| D6 | `/handoff` poll, note and toasts, `slash.ts:995-1017`; `use-prompt-actions/index.ts:558-640` | The 60 s poll, completion note and toasts stop after a switch and poll the session's owner | P2 P6 P9 | **OPEN** |

### E. Rewind, steering

| # | Path | Property | P | Test / status |
|---|---|---|---|---|
| E1 | Tile `beginRewind` rollback, `session-tile-actions.ts:209-222`, `:596-740` | Rolls back only its own turn, on the captured runtime | P2 P4 | done. T=`app/chat/tile-rewind-ownership.test.ts:35`. F=r29 |
| E2 | `runRewindSubmit` stamp and interrupt, `use-prompt-actions/rewind.ts:311`, `:358-420` | Nothing sent after its history read spans a switch | P6 | done. T=`tile-rewind-ownership.test.ts:106`. F=r29 |
| E3 | Tile survivor rebind, `session-tile-actions.ts:584-592` | Writes to the ticket's runtime, with the runtime half of the ticket too | P2 | **OPEN** |
| E4 | Tile regenerate/edit toasts, `:638`, `:735` | Ticketed | P9 | **OPEN** |
| E5 | Tile steer/redirect late discard and move, `:470-541` | Never recreates a retired runtime's state | P2 | **OPEN** |
| E6 | Primary reload/restore/edit rollbacks, `use-prompt-actions/index.ts:1021-1300` | Own turn only | P2 P4 | done. T=`late-consumers.test.ts:63`, `turn-generation.test.ts:64`. F=r28/r29 |
| E7 | Primary survivor rebind `applySurvivorRowIds`, `index.ts:971` (called `:1047`, `:1134`, `:1270`) | Ticketed, like the tile | P2 | **OPEN** |
| E8 | Primary edit stale-target retry, `index.ts:1243-1270` | The retry after `resumeStoredSession` carries the edit's stamp | P6 | **OPEN** |
| E9 | Primary regenerate/edit toasts, `index.ts:1074`, `:1296` | Ticketed | P9 | **OPEN** |
| E10 | Primary steering capture and recovery, `steering-session.ts:30-100`; `index.ts:817-968` | Refuses on disagreement; composite owner key | P3 | done. T=`steering-recovery.test.tsx:158`, `:207`, `:336`. F=r28b |
| E11 | Primary redirect optimistic discard, `index.ts:888`, `:892` | Ticketed | P2 | **OPEN** |

### F. Onboarding

| # | Path | Property | P | Test / status |
|---|---|---|---|---|
| F1 | Personalize and create boundaries, `app/contrib/onboarding-handoff.ts:200`, `:219` | No create after a switch during preparation | P6 | done. T=`onboarding-handoff-owner.test.ts:124`. F=r29 |
| F2 | Bind, `:244-305` | Adopts under the receipt owner; the hint and tile route follow an accepted bind | P1 P3 P5 | done. T=`onboarding-handoff-owner.test.ts:45`, `onboarding-handoff-ownership.test.ts:59`. F=r29 |
| F3 | Title and completion, `:313-330` | Nothing after the last await crosses a switch | P6 P9 | done. T=`onboarding-handoff-late.test.ts:45`. F=r28 |
| F4 | Error card and catch, `:359-418` | A superseded handoff writes no runtime state; a switch shows the error; a reconnect retries once | P2 P9 | done. T=`onboarding-handoff-epoch.test.ts:38`. F=r25 |
| F5 | Check-in hidden `prompt.submit`, `:436-444` | Ticketed, and sent to the session's owner | P2 P6 | **OPEN** |
| F6 | Receipt persistence, `:181-184` → `handoff-receipt.ts:66` | A receipt is never read back under a retired gateway epoch | P5 | **OPEN** |

### G. Stream events, reconnect, switch, readers

| # | Path | Property | P | Test / status |
|---|---|---|---|---|
| G1 | `session.reclaimed`, `use-message-stream/gateway-event/lifecycle.ts:92-120` | Fences every reply and write requested before it | P2 | done. T=`authority-boundary-r28.test.ts:114`, `session-reclaimed.test.tsx:59-151`, `submit-lifetime.test.ts:19`. F=r28/r29 |
| G2 | `message.start` turn generation, `message-stream.ts:136`; `session-state-cache.ts:172` | A new backend turn id is a new turn | P2 | done. T=`turn-generation.test.ts:64`. F=r29 |
| G3 | Primary reconnect fence, `app/gateway/hooks/use-gateway-boot.ts:457-464` | Fenced before the open socket is published | P9 | done. T=`use-gateway-boot.test.tsx:2701`. F=r28 |
| G4 | Secondary reopen, `store/gateway.ts:711-718` | Advances the fence for its scope | P9 | done. T=`gateway-connection-lifecycle.test.ts:324`, `runtime-authority.test.ts:121`, `:147`. F=r25 |
| G5 | Gateway switch wipe, `store/gateway-switch.ts:183-275` | Retires all identity state synchronously | P5 P9 | done. T=`gateway-switch.test.ts:149`, `gateway-epoch.test.ts:191`, `runtime-boundary.test.ts:178`. F=r25/r27 |
| G6 | Reconnect binding retention, `use-session-tile-delegate.ts:223` | A preserved tile keeps only its own owner's binding | P3 | done. T=`owner-keyed-bindings.test.ts:118`. F=r28b |
| G7 | Live-status poll, `contrib/hooks/use-background-sync.ts:757` → `:460`, `:508` | Identity never enters `$sessionStates` outside the authority, and a poll answered for a retired gateway recreates nothing. Today it publishes `{storedSessionId}` directly (verified at `:460`) | P1 P2 | **OPEN** |
| G8 | Background transcript reconcile, `use-background-sync.ts:191-230`, `:265-300` | Ticketed across a switch | P2 | **OPEN** (tests cover supersession only) |
| G9 | RPC dispatcher owner, `contrib/session-rpc-dispatcher.ts:69-100` | A runtime is routed to its exact owner | P3 | done. T=`ownership-boundary-r29.test.ts:46`, `session-rpc-dispatcher.test.ts:154`. F=r29 |
| G10 | Dispatcher owner probe → dispatch, `:106-118` | No dispatch after a switch during the probe | P6 | **OPEN** |
| G11 | Desktop notifications, `contrib/hooks/use-desktop-integrations.ts:321`, `:413` | Identity is resolved through the authority | P1 | **OPEN** |
| G12 | Tile `runtimeId` records (`patchSessionTile`, `storedSessionIdForRuntimeId` at `store/session-states.ts:1317`) | No second stored↔runtime record survives a switch or answers outside the authority | P1 | **OPEN** |

### H. Persisted state (the whole effect lifetime, P5/P9)

| # | Store | Property | P | Test / status |
|---|---|---|---|---|
| H1 | In-flight journal, `hooks/use-session-state-cache.ts:402` → `lib/inflight-turn-journal.ts:973` | Owner slot and epoch; epoch-less entries only until the first switch; a settle clears only its own turn | P5 | done. T=`inflight-turn-journal-owner.test.ts:35`, `:66`, `:75`, `inflight-turn-journal-scope.test.ts:18`. F=r28/r29 |
| H2 | Journal epoch at write time, `inflight-turn-journal.ts:997` | No retired-gateway content is journaled under the new epoch. This follows once E3, E5, E7, E11 and G8 are ticketed | P5 | **OPEN** |
| H3 | Transcript tail cache, `store/transcript-tail-cache.ts:166` (writers `index.ts:1706`, `:1887`, `:2234`) | A save that crosses a switch persists nothing; the key distinguishes owners the way owner keys do (today: REST scope, no `targetProfile`, no epoch) | P5 | **OPEN** (scope and wipe tested: `transcript-tail-cache.test.ts:72`, `:137`, `:145`) |
| H4 | Owner hints, `store/session.ts:1074` (writers `index.ts:683`, `:968`, `:2586`; `sdk/index.ts:986`, `:997`; `session-import/index.tsx:95`; `session.ts:1403`) | Two owners of one id do not collide; no hint from a retired operation | P3 P5 | **OPEN** |
| H5 | Read-only latch, `store/read-only-transcript.ts:28` (primary writer `index.ts:2324`) | Owner- and epoch-scoped; the writer is ticketed | P2 P5 | **OPEN** (the tile writer is tested: `runtime-boundary.test.ts:123`) |
| H6 | Composer queue, `store/composer-queue.ts:31` | Owner- and epoch-scoped | P5 | **OPEN** |
| H7 | Transcript backfill, `app/chat/transcript-backfill.ts` | Stamped; owner-keyed | P2 P3 | done. T=`transcript-backfill.test.ts:184`, `:249`. F=r25b |

### I. Other session RPCs

| # | Path | Property | P | Test / status |
|---|---|---|---|---|
| I1 | cwd set and model switch, `use-cwd-actions.ts:~94-115`; `use-model-controls.ts:~282-333` | Foreground writes and toasts are ticketed | P2 P9 | **OPEN** |

### J. Guards

| # | Mechanism | Property | P | Test / status |
|---|---|---|---|---|
| J1 | `requireOwner`, `runtime-bindings.ts:196` (used at `:292`, `:297`) | An owner-less `runtimeFor`/`validatedRuntimeFor` throws, whatever its form | P7 | done. T=`app/session/owner-required.test.ts`. F=r29 |
| J2 | Tile single-binding view (`runtime-bindings.ts:~743-760`), tile `getRuntimeIdForStoredSession` (`session-tile-actions.ts:343`) | They honour or require the owner | P3 P7 | **OPEN** |
| J3 | `adopt`, `forget`, untyped `acceptResumeReply`, the hint methods, and the hook's `getRuntimeIdForStoredSession` (`use-session-state-cache.ts:418`) | A missing owner is either required or explicitly the rule, per a decision (see "Needs Lake") | P3 P7 | **OPEN** |

### Completeness: what proves this inventory, and its limits

- **The census.**
  - **Where it is defined.** `eslint.config.mjs:45-145`, applied at `:217-218`. It is proven by `scripts/runtime-identity-census.test.ts`: 33 cases, including the controls.
  - **Scope.** `src/**/*.{ts,tsx}`, except tests, harnesses, `test-utils`, `src/test/**`, `src/plugins/**` (the plugin fence covers those) and the five authority files. `electron/` is not in scope.
  - **What it refuses:**
    - the raw map name;
    - the retired writer names;
    - owner-less `runtimeFor` / `validatedRuntimeFor` / `getRuntimeIdForStoredSession` / `forget` (fewer than 2 arguments), and `acceptResumeReply` (fewer than 4);
    - a literal dynamic import of authority or test modules;
    - borrowed `Map.prototype` mutators;
    - the restricted import names (registry mutators, recovery seams, `CachedRuntimeBindings` including type imports, `IDENTITY_WRITE` / `RETIRE_ALL`);
    - any test or harness module.
- **The runtime guard.** `requireOwner` applies to `BindingsCore.runtimeFor` and `validatedRuntimeFor` only.
- **The census and the runtime guard cover only what they recognise.** They cannot see:
  - **Imports:**
    - a variable or template `import()`, `import.meta.glob`, `require`;
    - a namespace import;
    - the unrestricted wrappers (`keepRecoveredRuntimeHint`, `takeRecoveredRuntimeHintFor`, `reclaimRuntime`, `advanceRuntimeFence`).
  - **Call forms:**
    - on `forget` and `acceptResumeReply`: `.call`, computed, extracted, or explicit `undefined`, since these have no runtime guard;
    - on `any`-typed receivers: TypeScript does not apply.
  - **Identity writes outside the authority:**
    - `adopt` with no evidence;
    - `ensureSessionState` / `updateSessionState` with a stored id;
    - direct `publishSessionState` / `$sessionStates` writes (`use-background-sync.ts:460`, `:508`; tile delegate `:309`; `index.ts:1307`, `:1699`);
    - `patchSessionTile({runtimeId})`, `storedSessionIdForRuntimeId`, `sessionOwnerByRuntimeId` (`session-states.ts:155`).
  - **Owner-blind or epoch-blind persisted stores:** rows H3–H6 and F6.
  - **Semantic properties.** No lint proves "ticket checked before a late write", "stamp checked after the last await" or "toast only while the ticket holds". Only the behavioural tests above do.
- **An independent source audit is invited to challenge this list.** The r29 review prompts ask for an exhaustive audit. Any path it finds becomes a new row here, classified by Part 3's outcomes.

**Discrepancies with `PATHS.md` that this inventory corrects:**
- **Row 16** says "existing suites" cover route resume, the queue drain and notifications. None has an owner or switch case (A22, A23, G11).
- **Row 7's "switch → return"** holds only relative to the flight stamp, which is taken after preparation (A3).
- **Row 3** cites `submit-gateway-epoch.test.ts:15`, which switches after the dispatch. A switch during the profile lookup is untested and unguarded (A16).


## Work list (the OPEN rows, grouped by the gap they close)

Each item extends the existing boundary to more sites. None adds a new mechanism. Each fix is proven by a red-first behavioural test at its row.

1. **First-dispatch stamps at the remaining sites (P6).** Rows: A3, A11, A16, A17, C4, C15, D3, D6, E8, F5, G10.
   - At each site, the operation's entry stamp is carried through its preparation.
   - It is checked before every request that follows an await.
   - A submit passes its stamp on to the attachment upload (C4).
2. **Tickets on the remaining late writes and persisted writes (P2, P5).** Rows: A4, A8, A21, A23, B3, E3, E5, E7, E11, G8, I1. H2 follows once these are closed.
3. **Tickets on late notifications and toasts (P9).** Rows: A4, A23, B5, B8, C8, C11, C14, D4, D5, D6, E4, E9, I1.
   - A toast after an await is shown only while its operation's ticket holds.
   - Alternatively, it is explicitly labelled as informational and writes no state.
4. **Owner propagation at the remaining sites (P3, P4).** Rows: A19, B4, B9, B10, C13, J2.
   - Recovery adoption carries the flight's owner and stamp.
   - Post-create YOLO uses the captured route.
   - Delete and archive forget under the removed session's owner.
   - The tile's attachment flight uses the composite key.
   - The tile view honours the owner.
5. **Tests for paths that already look right but have no behavioural test.** Rows: A9, A22, B5.
6. **Decided by Lake first ("Needs Lake" items 1–4):** G7, G11, G12, H3–H6, F6, J3.

**Counts:** 49 OPEN rows. Rows A4, A23, B5, D6 and I1 appear in two groups, so the group counts below sum to more than 49:

| Group | Rows |
|---|---|
| 1 | 11 |
| 2 | 11 (+ H2) |
| 3 | 13 |
| 4 | 6 |
| 5 | 3 |
| 6 | 9 |

## Stopping rule (proposed, like A28)

- **Scope is closed when all three hold:**
  - every Part 2 row is done or has a Lake ruling;
  - every Part 3 row has its outcome;
  - one Codex review and one Antigravity review of the closing commit find no new P1.
- **After Lake freezes this list,** a new discovery is classified by Part 3's outcomes. It is not added to scope automatically. A demonstrated production bug is still fixed, disproved, or explicitly deferred by Lake.
- **Reviews stay exhaustive audits,** per Lake's round-27 rule. Their findings are mapped to rows here rather than opening new rounds of scope.
- **Combined-release validation is a separate gate** (see Baseline).

## Needs Lake

**Lake's rulings, 2026-09-28 (all accepted):**
- **1. G7/G12:** the background poll and the tiles' runtime↔stored record go through the authority. They are not kept as mirrors.
- **2. H3–H6, F6:** the tail cache, owner hints, read-only latch, composer queue and handoff receipts are all scoped by owner and gateway epoch.
- **3. J3:** the owner is required on adopt, forget, acceptResumeReply, the hint methods and the hook lookup, with no default-owner fallback.
- **4. G11:** notifications resolve identity through the authority.
- **5. Accepted limit:** variable `import()`, `import.meta.glob` and `require` are a documented census limit.
- **6. Accepted:** an async-owner binding costs other callers one extra resume, and the single-lineage-owner rule stands.
- **7. Confirmed:** DISPOSITIONS residuals 1, 3 and 5 were accepted by Lake earlier. Update their wording.
- **8. Accepted limit:** older backends keep the offset-paging skips.
- **9.** The live evidence (G2, D8, D9, G5–G7, memory thresholds) runs at deploy acceptance, F4/F5.
- **10.** The coordinator integrates the release candidate, resolves cross-track overlaps, and owns the combined failure inventory and the electron rerun.
- **11.** Round-25 choice (b), the global in-flight fence, stays a known limit (decided earlier).


1. **G7 and G12: identity records outside the authority.**
   - **G7:** the background live-status poll publishes `{storedSessionId}` straight into `$sessionStates` (`use-background-sync.ts:460`, `:508`).
   - **G12:** `$sessionTiles` keeps a second runtime↔stored record, which survives the switch wipe.
   
   The options are:
   - (a) route both through the authority (a larger change);
   - (b) keep both as documented read-only mirrors, and add tests that neither can resurrect a retired runtime.
2. **H3–H6 and F6: other persisted stores.** These are the tail cache, owner hints, the read-only latch, the composer queue and handoff receipts. None is owner- and epoch-scoped as the journal now is. Which must be (P5), and which are accepted as switch-wiped only? The tail cache and receipts are wiped or guarded today; owner hints, the latch and the queue are not wiped at a switch.
3. **J3: optional-owner APIs.** `adopt`, `forget`, untyped `acceptResumeReply`, the hint methods and the hook's `getRuntimeIdForStoredSession` fall back to the defined rule when no owner is given. Should the owner be required on all of them, or should the rule fallback be accepted as the documented default?
4. **G11: desktop notifications** resolve identity through the tile list. Accept this, or route it through the authority?
5. **CX-r28-F18b: the census's documented limit** (variable `import()`, `import.meta.glob`, `require`). No reviewer or Lake acceptance is on record.
6. **Two judgement calls from the r28 follow-up, not yet accepted:**
   - A binding made under an asynchronously resolved owner is found only by callers that pass that owner. The others pay one extra resume.
   - The lineage rule trusts a continuation id's single lineage owner.
7. **The written residuals in `.t3-residuals-20260927/DISPOSITIONS.md`:**
   - §1, hidden steer with no queue is dropped (CX-r11-R1);
   - §3, keep upstream `resolveResumedBusy` (CX-r11-R3);
   - §5, legacy framing-marker ambiguity (CX-r11-R5).
   
   The r20-period handoff says Codex acknowledged Lake's acceptance, but the file still reads "needs Lake". Please confirm, so the wording can be updated.
8. **The old-backend offset-paging limit (CX-r13-OB).** It is documented only as a "Limit", and is not in the acceptance list.
9. **Live evidence and thresholds (T3-LIVE):** G2, D8, D9, G5, G6 and G7 on Lake's workload, and the proposed memory-watch thresholds. AG-r1-G6b and AG-r1-D8 have no repo test; accept them as live-check-only, or ask for tests.
10. **Integration ownership and cross-track overlaps (CX-r11-INT, CX-r9-N).** Also the non-T3 failure inventory (CX-r11-R6cd, CX-r17-N), and the electron project rerun (CX-r18-N). These belong to the combined-release gate.
11. **Lake's round-25 choice (b), the global in-flight fence, stays a known limit.** A resume in flight to a healthy connection is refused and retried once when a sibling connection reopens.

## Part 3: disposition of every T3 finding, rounds 1–28

**Outcomes:**
- **FIXED**: the named test passes at `44e26582a2`, and the named commit made it pass. "Superseded" means a later round reopened the area; the chain ends at the final fix.
- **OPEN**: names the Part 2 row or the gate that owns it.
- **DISPROVED**: with evidence.
- **NEEDS LAKE**: a decision or acceptance only Lake can give.

**Area tags:**
- **id** identity/resume;
- **tp** transcript paging;
- **cs** compaction summary (agent code);
- **gs** gateway status;
- **mem** renderer memory;
- **oth** composer, submit, rewind, reopen gate, flakes and hygiene.

The T3 track covered more than identity. Every area is dispositioned here; only the identity area has Part 1/2 rows.

**Sources and their gaps:**
- **Codex rounds 1–9:** the reports are in `E:\GitHub\Magi-AGI\hermes-agent-sync\`: `.t3-review-20260925` (round 1), `.t3-review-r2…r7-*`, `.t3-review-r8-precommit-20260926` and `.t3-review-r9-20260926`.
- **Antigravity rounds 1–9:** no report exists. The rows are rebuilt from the commits that cite Antigravity (`8858ea5ca6`, `180d9ab80e`, `2520e8c0d2`, `d5ee44d843`, `ec82553019`), and the rounds with no traceable ID are grouped.
- **Rounds 10–18:** `.t3-review-r10…r18` in the worktree.
- **Residuals:** DISPOSITIONS.md items 1–6 are Codex's round-11 recommendations, listed here as CX-r11-R1…R6.
- **Test runs:**
  - Python tests (`tests/agent`, `tests/hermes_state`, `tests/tui_gateway`, `tests/hermes_cli`, `tests/plugins`) passed at their fix commits per those commits' messages. Codex re-ran them in rounds 11–16. They were not re-run at `44e26582a2`: no backend code changed after round 16.
  - `web/` and `electron/` tests are named by file. The electron project's full rerun is still open (CX-r18-N).

### Round-0 track items (fixed before review round 1)

| id | area | finding | outcome | evidence |
|---|---|---|---|---|
| T3-A4-R1 | oth | The reopen gate was released during an in-flight reload | FIXED | `5e62a8e94f`; `electron/session-reopen-gate.test.ts` "a reload in flight holds the queue… (R-A4-1)" |
| T3-A4-R2 | oth | A report with a null token matched when no document was current | FIXED | `5e62a8e94f`; same file "(R-A4-2)" |
| T3-G7-1 | id | A detach erased the stored→runtime binding | FIXED, superseded | `64bf7f9095`, then CX-r1-#4 and the identity class. `use-session-state-cache.test.tsx` "another runtime letting go … does not erase…" |
| T3-G7-2 | oth | A refused steer dropped the typed words | FIXED, superseded | `64bf7f9095`, then CX-r1-#1 (`8858ea5ca6`) |
| T3-G7-3 | oth | The rewind content match mis-aimed (the "244→2" cut) | FIXED, superseded | `64bf7f9095`, then CX-r1-#2 (`aeb85f4f75`) |
| T3-G6-1 | gs | The reconnect edge was consumed while another view was showing | FIXED | `94eac00c26`; `use-route-resume.test.tsx` "a reconnect that lands while another view is showing still reattaches…" |
| T3-G6-2 | gs | The resume retry ladder gave up at about 15 s | FIXED | `94eac00c26`, jitter in `180d9ab80e`; `use-route-resume.test.tsx` "a stranded window keeps retrying through a backend stall…" |
| T3-G6-3 | gs | No trace attributed a stranded window | FIXED | `94eac00c26`; `electron/renderer-log.test.ts` "passes the session-attach trace…" |
| T3-G6-4 | gs | Stale persistent "gateway down" toast (a cherry-pick of `41f45ec62f`, Lake approved) | FIXED, superseded | `9023b47a99`, then CX-r6-#2. `use-gateway-boot.test.tsx` "the persistent lost-connection warning is retired…" |
| T3-G6-5 | gs | A focused window's parked active scope was never redialed (Lake approved) | FIXED | `34c3493622`; `gateway-connection-lifecycle.test.ts` "a focus nudge redials only the ACTIVE retired scope, at foreground priority" |
| T3-G5-1 | mem | The tool-diff store was unbounded | FIXED, superseded | `7372da8c3a`, then CX-r1-#3 (`1d97bd31a1`) |
| T3-G5-2 | mem | The markdown block cache was unbounded by size | FIXED | `7372da8c3a`; `lib/markdown-blocks.test.ts` "holds a bounded amount of text while a long reply streams" |
| T3-G5-3 | mem | Renderer OOMs could not be attributed to a window | FIXED (diagnostic) | `cf62332d6d`; `electron/renderer-memory.test.ts` |
| T3-G5-4 | cs | The previous compaction summary grew on every pass | FIXED, superseded | `744aaaf778`, then the CX-r6…r12 trimming chain; final state `c5084be5d6`, `519f98d9c4`, `e5f85d1b5f` |
| T3-G5-5 | cs | Live checkpoints were cut mid-sentence | FIXED | The causes were fixed: the quoted marker (`93e19b7981`) and the reasoning cap (`d5ee44d843`). Codex r7 advised against a heading-count floor |
| T3-LIVE | gs/mem/oth | Live G2, D8, D9, G5, G6 and G7 checks; the memory thresholds are proposals | NEEDS LAKE | Scripts `scripts/live-checks/desktop/*.ps1` (`9ac6133c0c`, `765a901db0`). Every review through r18 says the live evidence is still open |

### Codex, rounds 1–18

| id | round | area | finding | outcome | evidence |
|---|---|---|---|---|---|
| CX-r1-#1 | 1 | oth | A refused steer overwrote another chat's live draft | FIXED | `8858ea5ca6`; `use-composer-submit.test.tsx` "a steer refused after the user switched chats never touches the visible draft" |
| CX-r1-#2 | 1 | oth | The newest-turn rewind guard compared divergent ordinal spaces | FIXED | `aeb85f4f75`; `rewind.test.ts` "regenerating the newest rendered turn never cuts at an older durable turn…" |
| CX-r1-#3 | 1 | mem | Computed tool-diff atoms pinned whole-map snapshots | FIXED | `1d97bd31a1`; `store/tool-diffs.test.ts` "drops the atom of an unobserved row that holds no diff" |
| CX-r1-#4 | 1 | id | The ownership guard covered only the unchanged-id branch | FIXED, superseded | `c01bbe0bb2`, then CX-r2-#1 and the identity class (final: Part 3, rounds 19–28). `use-session-state-cache.test.tsx` "a late event that recreates a stale runtime state does not take the binding" |
| CX-r2-#1 | 2 | id | Authoritative resumes and recoveries were not adopted (a regression) | FIXED, superseded | `b3302b11f9`, `02cda2d437`; `stored-session-adoption.test.tsx` "a prompt submit recovery binds the recovered runtime (%s)" and "a tile resume binds its new runtime…" |
| CX-r2-#2 | 2 | oth | A refused ordinary send overwrote another chat's composer | FIXED | `2520e8c0d2`; `use-composer-submit.test.tsx` "refused after the user switched chats: the visible draft is untouched…" |
| CX-r2-NB | 2 | oth | A hidden user row after the rewind target cleared "newest" | FIXED | `2520e8c0d2`; `rewind.test.ts` "a hidden user row after the target does not make it an older turn" |
| CX-r2-PC1 | 2 | gs | The toast cherry-pick's REST probe must describe the failed gateway | FIXED | `9023b47a99`; `use-gateway-boot.test.tsx` "never probes another backend when a secondary is foregrounded" |
| CX-r2-PC2 | 2 | gs | Focus re-arms only the active parked scope | FIXED | `34c3493622` (Lake approved); as T3-G6-5 |
| CX-r3-#1 | 3 | oth | A refused first send was restored under the obsolete `__new__` scope | FIXED, superseded | `991390cb01`, then CX-r4-#1 and CX-r5-#1; final `c38e1e5248`. `first-send-restore.integration.test.tsx` |
| CX-r3-#2 | 3 | id | Slash target resolution did not adopt its runtime | FIXED, superseded | `081b1113bd`; `resolve-target-session.test.ts` "adopts a resumed runtime for the targeted stored session…". Reworked in the identity class |
| CX-r3-#3 | 3 | id | The onboarding retry relied on implicit adoption | FIXED, superseded | `081b1113bd` (it had only a Codex scratch probe at the time). Covered now by `onboarding-handoff-owner.test.ts` and `onboarding-handoff-ownership.test.ts` |
| CX-r4-#1 | 4 | oth | A later chat's assignment captured an earlier refused send | FIXED, superseded | `87c1a878ae`, then CX-r5-#1. `use-composer-submit.test.tsx` "a later chat created while the first send is pending never receives its words" |
| CX-r5-#1 | 5 | oth | The assignment was recorded after the post-create (YOLO) drift abort | FIXED | `c38e1e5248`; `first-send-restore.integration.test.tsx` "a switch during the real post-create YOLO RPC preserves the first send…" |
| CX-r6-#1 | 6 | cs | Trimming dropped the newest action or split its continuation | FIXED, superseded | `35e179febb`, then CX-r7-#2. `tests/agent/test_context_compressor_previous.py::test_the_newest_action_survives_whole_even_with_a_trailing_newline` |
| CX-r6-#2 | 6 | gs | The outage toast survived focus, online and manual recovery | FIXED | `0200dcfe0d`; `use-gateway-boot.test.tsx` "retires the outage warning after a %s recovery" |
| CX-r6-#3 | 6 | cs | A quoted end marker cut the summary | FIXED | `93e19b7981`; `tests/agent/test_compaction_framing_markers.py::test_a_summary_quoting_the_framing_keeps_every_section` |
| CX-r6-N | 6 | cs | The 8,000-char reasoning cap truncates | FIXED | Raised as CX-r7-#3; `d5ee44d843` |
| CX-r7-Q | 7 | cs | A column-0 code block started a new entry | FIXED | `369ece9ac0`; `…previous.py::test_a_code_block_opened_at_column_zero_stays_with_its_action` |
| CX-r7-#1 | 7 | cs | The deterministic fallback re-added framing markers | FIXED | `d5ee44d843`; `test_compaction_framing_markers.py::test_the_static_fallback_survives_rehydration_after_a_user_quotes_the_framing` |
| CX-r7-#2 | 7 | cs | Column 0 is not a reliable entry boundary | FIXED, superseded | `d5ee44d843`, then CX-r8-#1. `…previous.py::test_the_newest_action_keeps_a_lazy_unindented_continuation` |
| CX-r7-#3 | 7 | cs | A reasoning-only summary was sliced at 8,000 chars and persisted as complete | FIXED | `d5ee44d843`; `test_context_compressor_reasoning_fallback.py::test_oversized_reasoning_fallback_never_becomes_a_checkpoint` |
| CX-r7-#4 | 7 | cs | The legacy reader cut at an inline quoted marker | FIXED | `d5ee44d843`; `test_compaction_framing_markers.py::test_a_legacy_inline_quote_keeps_the_body_and_excludes_the_live_tail`. Ambiguous standalone copies: CX-r11-R5 |
| CX-r8-#1 | 8 | cs | Fence tracking ignored tilde and longer fences | FIXED, superseded | `fb009792af`, then CX-r9-#1. `…previous.py::test_the_newest_action_keeps_its_whole_fenced_example` |
| CX-r8-#2 | 8 | cs | The preview SQL split at the first INSTR on merged carriers | FIXED | `fb009792af`; `tests/hermes_state/test_preview_quoted_framing.py::test_the_preview_of_a_merged_carrier_is_the_users_own_text` |
| CX-r8-#3 | 8 | cs | The holographic memory plugin split at the first delimiter copy | FIXED | `fb009792af`; `tests/plugins/memory/test_holographic_quoted_framing.py::test_a_decision_after_an_inline_quote_is_still_extracted` |
| CX-r9-#1 | 9 | cs | An indented literal fence swallowed protected sections | FIXED, superseded | `ec82553019`, then CX-r10-#1. `…previous.py::test_an_indented_literal_delimiter_does_not_hide_the_following_sections` |
| CX-r9-#2 | 9 | cs | The dashboard `splitCompactionContent` used the first `indexOf` | FIXED | `ec82553019`; `web/src/lib/compaction-split.test.ts` |
| CX-r9-N | 9, 13 | oth | Integration overlaps to preserve (`hermes_state_common.py`, `hermes_state_messages.py`, `prompt_turn.py` with the mcp-hygiene, gateway-3 and lifecycle tracks) | OPEN | The combined-release gate (Baseline). No integration has been done |
| CX-r10-#1 | 10 | cs | A tab counted as one column, so Active State could be trimmed | FIXED | `c5084be5d6`; `…previous.py::test_an_indented_literal_delimiter_never_makes_active_state_trimmable` |
| CX-r10-#2 | 10 | cs | An unclosed fence read as literal lost the newest entry | FIXED | `c5084be5d6`; `…previous.py::test_the_newest_action_keeps_its_whole_unclosed_example` |
| CX-r11-R1 | 11 | oth | A hidden steer with no queue is dropped (by design) | NEEDS LAKE | DISPOSITIONS §1: proposed ACCEPT. The r20-period handoff says Codex acknowledged Lake's acceptance, but DISPOSITIONS still reads "needs Lake" |
| CX-r11-R2 | 11 | tp | "Show earlier" skipped released rows | FIXED, superseded | `fa3d651fdc` was withdrawn after CX-r12. Redesigned as row-id paging: `a939e99bf3`, `fc70065bd4`, then the r13–r17 chain. `app/chat/transcript-lifecycle.test.ts`, `stale-recovery-race.test.ts` |
| CX-r11-R3 | 11 | gs | Keep upstream `resolveResumedBusy` (#70449) | NEEDS LAKE | DISPOSITIONS §3, plus live G6/G7 verification |
| CX-r11-R4 | 11 | cs | The session-log date label was lost when trimming | FIXED, superseded | `519f98d9c4`, then CX-r12-P2 |
| CX-r11-R5 | 11 | cs | Legacy rows with a standalone quoted framing marker are ambiguous | NEEDS LAKE | DISPOSITIONS §5: proposed accept as a legacy limit |
| CX-r11-R6a | 11 | cs | Stall fallback test flake | FIXED | `817117f2fc`; the production race was fixed in `645dd3f06b` (CX-r12-FL) |
| CX-r11-R6b | 11 | oth | custom-endpoints "Applies-to" test flake | FIXED | `817117f2fc`; `settings/custom-endpoints-settings.test.tsx` |
| CX-r11-R6cd | 11, 12 | oth | Non-T3 failures: electron git/pool, hermes-bots, compute_host, sessions router | OPEN | DISPOSITIONS §6c–d: a candidate failure inventory with owners (combined-release gate) |
| CX-r11-INT | 11 | oth | Integrate the full range and validate the immutable candidate | NEEDS LAKE | Who owns the integration into `magi/sync-v2026.9.21` |
| CX-r12-1a | 12 | tp | A cold reopen inherited an obsolete fully-loaded flag | FIXED | `fc70065bd4`; `transcript-lifecycle.test.ts` "offers older history again when a fully loaded session comes back through a cold tail hydration" |
| CX-r12-1b | 12 | tp | Refreshes reset backfill progress | FIXED | `a939e99bf3`, `fc70065bd4`; `transcript-lifecycle.test.ts` "reaches the oldest row even when an unapplied tail re-read lands between clicks" |
| CX-r12-1c | 12 | tp | A discarded-refresh race skipped a released streamed answer | FIXED | `fc70065bd4`; `transcript-lifecycle.test.ts` it.each cases |
| CX-r12-P2 | 12 | cs | A blank line hid the date label; a mixed layout borrowed the wrong day | FIXED | `e5f85d1b5f`; `…previous.py::test_a_blank_line_after_the_newest_date_never_borrows_the_previous_days_label` |
| CX-r12-FL | 12 | cs | The stall test was still intermittent | FIXED | `645dd3f06b`; `tests/agent/test_compression_worker_deadline_race.py::test_an_over_window_worker_that_aborts_at_the_deadline_escalates_to_the_fallback` |
| CX-r12-HY | 12 | oth | Untracked scratch in the shared worktree | OPEN | The `.t3-review-*` and `.t3-fix-*` folders are still untracked in the worktree. They are cleaned before integration (combined-release gate) |
| CX-r13-#1 | 13 | tp | A completion stamped the previous turn's final bubble | FIXED, superseded | `a232a2368d`, then CX-r14-#1. `use-message-stream/persisted-row-identity.test.tsx` |
| CX-r13-#2 | 13 | tp | The backend reported the wrong rows across compaction | FIXED | `b2a87098b6`; `tests/tui_gateway/test_message_complete_persisted_rows.py` (rotated / in-place cases) |
| CX-r13-#3 | 13 | tp | Retention discarded an index-zero anchor | FIXED | `a232a2368d`; `transcript-lifecycle.test.ts` "keeps a turn whole when its first message is the only exact anchor" |
| CX-r13-#4 | 13 | tp | A transient anchored-read error fell back to offsets | FIXED | `a232a2368d`; `transcript-lifecycle.test.ts` "one failed anchored read" |
| CX-r13-#5 | 13 | tp | `persisted` was not in the event contract | FIXED | `b2a87098b6`; `test_message_complete_persisted_rows.py::test_the_completion_frame_carries_persisted_rows_through_the_event_contract` |
| CX-r13-#6 | 13 | tp | An unknown anchor returned 200 in the default projection | FIXED | `b2a87098b6`; `tests/hermes_state/test_get_messages_before_anchor.py`, `tests/hermes_cli/test_session_messages_before_row.py` |
| CX-r13-#7 | 13, 15 | oth | The stall test and `test_tui_gateway_server` attempt outbound network lookups | OPEN | DISPOSITIONS §6e; the owner is test hygiene |
| CX-r13-OB | 13 | tp | An old backend without `before_row_id`: the offset path keeps the r12 1b/1c skips | NEEDS LAKE | Documented only as a "Limit" in DISPOSITIONS §2. It is not in the acceptance list |
| CX-r14-#1 | 14 | tp | A stale turn anchor after an error, settle, adopted turn or reconnect gap | FIXED, superseded | `ff2820a77a`, `b259be24d2`, then CX-r15-#1. `persisted-row-identity.test.tsx`; `test_message_complete_persisted_rows.py::test_message_start_names_its_turn_within_the_event_contract` |
| CX-r14-#2 | 14 | tp | The refresh signature ignored row ids, so "Show earlier" stuck | FIXED | `b259be24d2`; `contrib/hooks/background-refresh-row-identity.test.ts`; `transcript-lifecycle.test.ts` "replaces a store whose ids the backend rewrote…" |
| CX-r15-#1 | 15 | tp | A new turn stamped an unanswered older prompt with its user row | FIXED | `cb4ec86e74`, `02084fbb19`; `persisted-row-identity.test.tsx` "never gives an unanswered older prompt the user row…"; `tests/tui_gateway/test_submit_time_user_row.py::test_the_submit_reply_names_the_row_it_wrote` |
| CX-r15-#2 | 15 | tp | A delayed stale-anchor recovery overwrote a newer transcript | FIXED | `02084fbb19`, completed by `1df2723e52`; `transcript-lifecycle.test.ts` "drops a stale-anchor recovery that a newer transcript overtook…" |
| CX-r15-N | 15 | oth | Two `test_tui_gateway_server.py` failures | DISPROVED | Not a T3 regression: Codex reproduced both on base `242bad5ab4` (`python-failures-isolated.log`) |
| CX-r16-#1 | 16 | tp | Stale recovery keyed on the durable span and the lagging view | FIXED | `1df2723e52`; `app/chat/stale-recovery-race.test.ts` (3 cases) |
| CX-r16-#2 | 16 | tp/id | Submit read `user_row_id` off the envelope; the recovered runtime lacked the message | FIXED, superseded | `1df2723e52`, then CX-r17-#1/#2. `use-prompt-actions/submit-row-reply.test.ts` |
| CX-r17-#1 | 17 | id | A late submit acknowledgement reversed a compaction's rotation | FIXED, superseded | `2661bfa886`, then CX-r19-F1 and CX-r20-F1 (`ee45735a89`). `submit-late-ack.test.ts` "a delayed submit acknowledgement cannot rotate a compacted runtime back…" |
| CX-r17-#2 | 17 | oth | The optimistic message was carried too late; attachment recovery missed | FIXED, superseded | `2661bfa886`, then CX-r18-#1. `submit-late-ack.test.ts` ×2 |
| CX-r17-#3 | 17 | oth | A recovered hidden submit was shown as a visible bubble | FIXED | `2661bfa886`; `submit-late-ack.test.ts` "a hidden submit remains hidden when its accepted runtime was recovered" |
| CX-r17-N | 17 | oth | Six settings/free-tier failures from the broad run were not dispositioned | OPEN | Needs an entry in the §6 failure inventory (combined-release gate). Overlap with §6c is unverified |
| CX-r18-#1 | 18 | oth | A reused runtime kept `interrupted=true` and dropped the new answer | FIXED, superseded | `2b50c25be4`, then CX-r19-F2 (`f3aeba2232`). `submit-handoff.test.ts` |
| CX-r18-#2 | 18 | id | Tile recovery was not adopted into the shared cache | FIXED, superseded | `2b50c25be4`, then CX-r19-F3 and the identity class. `app/chat/session-tile-recovery.test.ts` |
| CX-r18-#3 | 18 | oth | A refused retry's error landed on the abandoned runtime | FIXED, superseded | `2b50c25be4`, then CX-r19-F1/F4 (`f3aeba2232`). `submit-handoff.test.ts` |
| CX-r18-N | 18 | oth | The electron vitest project rerun is still pending | OPEN | The combined-release gate. No evidence of a full rerun since it stalled on WMI timeouts |

### Antigravity, rounds 1–18

| id | round | area | finding | outcome | evidence |
|---|---|---|---|---|---|
| AG-r1-a | 1 | oth | Steer scope must be captured at steer time | FIXED | `8858ea5ca6`; `use-composer-submit.test.tsx` switched-chat steer cases |
| AG-r1-#4 | 1 | oth | A button-sourced busy external submit dropped its words when there was no queue | FIXED | `8858ea5ca6`; `use-composer-submit.test.tsx` "a refused busy external submit with no queue keeps its words in the composer" |
| AG-r1-G6a | 1 | gs | Retries run in lockstep across 20–27 windows; add jitter | FIXED | `180d9ab80e`; `use-route-resume.test.tsx` "…(jitter %s)" |
| AG-r1-G6b | 1 | gs | The exhaustion trace repeated on every effect re-run | FIXED, no repo test | `180d9ab80e`. Codex r2 verified the latch by inspection only. A repo test is missing. This is the gateway-status area, outside Part 2's identity rows; see "Needs Lake" item 9 |
| AG-r1-D8 | 1 | mem | Memory-watch script: a killed teardown should be INFO; total bound 11,000 MB | FIXED, no repo test | `180d9ab80e` (`scripts/live-checks/desktop/renderer-memory-watch.ps1`). Codex r2 confirmed with scratch fixtures. The threshold is under T3-LIVE |
| AG-r2-NB | 2 | oth | A hidden user row after the rewind target | FIXED | `2520e8c0d2`; as CX-r2-NB |
| AG-r3–6, r8 | 3–6, 8 | — | Grouped: IDs not recoverable. No Antigravity report was found and no commit cites one | OPEN (provenance only) | No known unaddressed finding. The reports themselves are missing |
| AG-r7 | 7 | cs | Traced both live cut-off checkpoints to the reasoning cap | FIXED | `d5ee44d843`; as CX-r7-#3 |
| AG-r9 | 9 | cs | The preview SQL must accept CRLF framing | FIXED | `ec82553019`; `test_preview_quoted_framing.py::test_a_carrier_with_crlf_line_ends_previews_the_same_as_the_python_readers` |
| AG-r10-#1 | 10 | cs | Tab indentation swallows Active State | FIXED | `c5084be5d6`; as CX-r10-#1 |
| AG-r10-#2 | 10 | cs | An unclosed fence fragments the newest entry | FIXED | `c5084be5d6`; as CX-r10-#2 |

Antigravity approved rounds 11–18 with no findings. On the same heads, Codex found P1/P2 blockers that later fixes addressed. Those approvals are therefore not relied on as evidence.

**Counts, rounds 1–18:**
- Codex: 69 rows: 57 FIXED, 6 OPEN, 5 NEEDS LAKE, 1 DISPROVED.
- Antigravity: 11 rows: 10 FIXED (2 without a repo test), 1 OPEN (provenance only).
- Round-0 items: 16 rows: 15 FIXED, 1 NEEDS LAKE.

### Rounds 19–28 (reviews `.t3-review-r19…r28`)

About the reviews in this range:
- r19 has a Codex review only.
- r20–r24 and r26 have both a Codex and an Antigravity review.
- r27 and r28 have an exhaustive Codex audit only.
- There is no r25 review: round 25 was the refactor `6a483bc60f`, which answered r24.

Every Antigravity finding here restates a Codex finding from the same round.

A test cited here is a path under `apps/desktop/src/` unless shown otherwise. All of them are tracked at `44e26582a2` and pass in the full ui run. The census test runs in the electron project: 33/33.

"Superseded" means a later round reopened the same area. The chain ends at the named final fix and test.

#### Codex (65 findings)

| id | finding | outcome | evidence (fix commit, then test) |
|---|---|---|---|
| CX-r19-F1 | A late failed retry re-binds a compacted runtime to the old stored id | FIXED, superseded | `f3aeba2232`, reopened by CX-r20-F1. Final fix `ee45735a89`. Tests: `session/hooks/use-prompt-actions/submit-handoff.test.ts` "a late refused retry on the recovered runtime cannot undo a compaction rotation", and `submit-ownership.test.ts` |
| CX-r19-F2 | Reseeding a recovered runtime splits a streaming answer | FIXED | `f3aeba2232`. `submit-handoff.test.ts` "a handoff to a runtime that is still streaming leaves that answer in its own bubble" |
| CX-r19-F3 | Tile file/image recovery publishes its binding after the upload retry | FIXED | `f3aeba2232`. `app/chat/session-tile-attachment-recovery.test.ts` (text/file/image) |
| CX-r19-F4 | A primary attachment retry failure lands on the abandoned runtime | FIXED | `f3aeba2232`. `submit-handoff.test.ts` "a refused attachment retry puts its error on the recovered runtime" |
| CX-r20-F1 | A late write re-binds R after the reverse map handed B to a newer runtime | FIXED | `ee45735a89`. `submit-ownership.test.ts` "a late write cannot roll back a compacted runtime after a newer runtime took over the rotated id" |
| CX-r20-F2 | Handoff adopts a stale recovery-cache entry before any guard | FIXED, superseded | Chain: `ee45735a89`, then r21-F1/F3, `14fda1ed22`, r22-F1/F2, `59e0b56d62`, r23-F1, `d224fe593b`, and finally the authority in `6a483bc60f`. Test: `submit-ownership.test.ts` "a recovery never adopts a runtime that has since rotated to a different stored id" |
| CX-r20-F3 | Foreground `$busy` is cleared while the turn is live | FIXED | `ee45735a89`. `submit-ownership.test.ts` "a failed recovered send does not clear foreground busy while that runtime is streaming" and "a refused queue drain cannot leave the foreground idle…" |
| CX-r20-F4 | A queue busy refusal leaves an optimistic question behind | FIXED | `ee45735a89`. `submit-ownership.test.ts` "a refused queue drain leaves no unsent question…" |
| CX-r21-F1 | The symmetric adoption gate blocks a forward resume after a missed compression | FIXED, superseded | `14fda1ed22`, then r22-F1, then `59e0b56d62`. Test: `session/hooks/runtime-lineage.test.ts` "binds a backend-confirmed newer stored id after this window missed the compression event" |
| CX-r21-F2 | Tile warm/cold completions write the old id before the gate | FIXED | `14fda1ed22`. `runtime-lineage.test.ts` "a late %s tile resume cannot roll back…" (warm/cold). Onboarding part: CX-r22-F3 |
| CX-r21-F3 | LRU eviction erases the gate's evidence | FIXED, superseded | `14fda1ed22`, then r22-F2, then `59e0b56d62`. Tests: `runtime-lineage.test.ts` eviction cases ×2 |
| CX-r22-F1 | A confirmed reply for an unseen intermediate id rolls C back to B | FIXED, superseded | `59e0b56d62`, then r23-F1, then `d224fe593b`. Test: `runtime-lineage.test.ts` "a delayed confirmed reply for an unseen intermediate id cannot roll back a newer binding" |
| CX-r22-F2 | Eviction forgets the current owner, so a late unconfirmed claim binds | FIXED | `59e0b56d62`. `runtime-lineage.test.ts` "an in-flight unconfirmed recovery does not gain authority from transcript eviction" |
| CX-r22-F3 | Onboarding overwrites or settles a live turn and ignores a refused adoption | FIXED | `59e0b56d62`. `app/contrib/onboarding-handoff-ownership.test.ts` ×3 |
| CX-r23-F1 | A late single-flight joiner gives an old reply a new clock | FIXED | `d224fe593b`. `session/hooks/resume-ordering.test.ts` "joining an older resume flight cannot give its reply a newer identity clock" |
| CX-r23-F2 | A switch's registry clear does not fence pending completions | FIXED, superseded | `d224fe593b`, then r24-F1–F3, then `6a483bc60f`. Test: `resume-ordering.test.ts` "an old gateway tile reply waiting on REST cannot repopulate the reset identity registry" |
| CX-r23-F3 | The primary resume completion writes identity without an ordering check | FIXED | `d224fe593b`. `resume-ordering.test.ts` "the primary resume completion also rejects a runtime identity newer than its request" |
| CX-r24-F1 | An old gateway reply still authorizes resolver, recovery, submit and onboarding after a switch | FIXED, superseded | `6a483bc60f`, then r27-F02 (`fe4b930d99`), then r28-F02 (`44e26582a2`). Tests: `session/gateway-epoch.test.ts` ×4, `use-prompt-actions/submit-gateway-epoch.test.ts` ×2, `app/contrib/onboarding-handoff-epoch.test.ts` |
| CX-r24-F2 | Lazy cache clearing does not guard reads or warm publication | FIXED, superseded | `6a483bc60f`, then r26-F6 (`1e8a6aa20b`). Tests: `gateway-epoch.test.ts` "the first lookup after a gateway wipe…" and "a warm tile cannot republish…" |
| CX-r24-F3 | Reconnect/reopen advance no identity generation | FIXED, superseded | `6a483bc60f`, then r26-F3/F4 (`1e8a6aa20b`), then r27-F15 (`fe4b930d99`). Test: `gateway-epoch.test.ts` "reconnect retirement also invalidates the old stamped tile reply…" |
| CX-r24-F4 | Cold resume ignores the `session_key`/`resumed` continuation | FIXED | `6a483bc60f`. `gateway-epoch.test.ts` "primary resume binds the continuation reported by the real %s response shape" |
| CX-r26-F1 | A branch sends a parent transcript read before a switch | FIXED, superseded | `1e8a6aa20b`, then r27-F01 (`fe4b930d99`), then r28-F02 (`44e26582a2`). Tests: `session/hooks/use-session-actions/gateway-epoch-completions.test.tsx` "a branch cannot copy a parent transcript…", and `use-session-actions/pre-dispatch.test.ts` "R27 F1 …" ×2 |
| CX-r26-F2 | Tile read-only fallback and warm refresh bypass acceptance | FIXED | `1e8a6aa20b`. `session/runtime-boundary.test.ts` ×2 |
| CX-r26-F3 | Event-log truncation turns a switch abort into a retry | FIXED | `1e8a6aa20b`. `runtime-boundary.test.ts` "a gateway switch stays an abort after the diagnostic event log wraps" |
| CX-r26-F4 | A create reply is accepted after a reconnect retired its runtime | FIXED | `1e8a6aa20b`. `gateway-epoch-completions.test.tsx` "a create reply from the runtime retired by a reconnect…" |
| CX-r26-F5 | The cache, store, journal and return value carry different identities | FIXED, superseded | `1e8a6aa20b`, then r27-F10. Tests: `runtime-boundary.test.ts` "a guarded content write publishes and returns the same kept identity…", and `pre-dispatch.test.ts` "R27 F2 an identity-only update publishes…" |
| CX-r26-F6 | The tile single-binding view is a second, unguarded authority | FIXED, superseded | `1e8a6aa20b`, then r27-F12. Tests: `runtime-boundary.test.ts` and `session/authority-boundary-r28.test.ts` "a tile view must honor the owning authority…" |
| CX-r26-F7 | The census replaces the plugin fence and misses bypasses | FIXED, superseded | `1e8a6aa20b`, then r27-F17, then r28-F18. `apps/desktop/scripts/runtime-identity-census.test.ts` |
| CX-r27-F01 | The stamp is checked before the owner-handshake await, not at dispatch | FIXED, superseded | `fe4b930d99`, then r28-F02. `pre-dispatch.test.ts` "R27 F1 branch/create refuses before dispatch…" |
| CX-r27-F02 | An old RPC's not-found starts a fresh recovery after a switch | FIXED, superseded | `fe4b930d99`, then r28-F02 (the first call). `use-prompt-actions/recovery-boundary.test.ts` "a not-found reply to an old-gateway RPC cannot begin recovery on the new gateway" |
| CX-r27-F03 | A successful slash completion recreates state across a switch | FIXED, superseded | `fe4b930d99`, then r28-F05/F08. `use-prompt-actions/late-consumers.test.ts` "a slash completion cannot recreate its retired runtime…" |
| CX-r27-F04 | A late rewind failure overwrites a newer running turn | FIXED, superseded | `fe4b930d99`, then r28-F03/F04. `late-consumers.test.ts` "a failed rewind cannot roll back a newer running turn" |
| CX-r27-F05 | Shared resumes coalesce different owners of one durable id | FIXED, superseded | `fe4b930d99` and `4ba5f4131e`, then r28-F01/F10/F11. Tests: `recovery-boundary.test.ts` "two owners of the same durable id…", and `session/owner-keyed-bindings.test.ts` |
| CX-r27-F06 | The persisted turn journal has no owner namespace | FIXED, superseded | `fe4b930d99` (epoch), then r28-F12/F13 (`44e26582a2`). `lib/inflight-turn-journal-scope.test.ts`, `lib/inflight-turn-journal-owner.test.ts` |
| CX-r27-F07 | Identity and transcript selection disagree on continuation replies | FIXED | `fe4b930d99`. `pre-dispatch.test.ts` "a primary resume uses its named continuation…", and `app/contrib/hooks/tile-continuation.test.ts`. Onboarding counterpart: r28-F16 |
| CX-r27-F08 | A cold resume's old running snapshot revives an ended turn | FIXED | `fe4b930d99`. `pre-dispatch.test.ts` "a cold resume snapshot cannot restart a turn…". Warm sibling: r28-F09 |
| CX-r27-F09 | Onboarding's title await is unfenced; the guide is notified after a switch | FIXED | `fe4b930d99`. `app/contrib/onboarding-handoff-late.test.ts` |
| CX-r27-F10 | An identity-only update skips publication | FIXED | `fe4b930d99`. `pre-dispatch.test.ts` "R27 F2 an identity-only update publishes the identity it returns" (the title says F2; the test covers F10) |
| CX-r27-F11 | Mutating an updater input bypasses the identity guard | FIXED | `fe4b930d99`. `authority-boundary-r28.test.ts` "mutating an updater input…" |
| CX-r27-F12 | The tile view trusts stale refs after reconnect | FIXED | `fe4b930d99`. `authority-boundary-r28.test.ts` "a tile view must honor the owning authority…" |
| CX-r27-F13 | Cold display hydration has an ABA gap across a switch | FIXED | `fe4b930d99`. `authority-boundary-r28.test.ts` "a cold display hydration cannot become owned again…" |
| CX-r27-F14 | `session.reclaimed` does not fence the runtime's pending reply | FIXED, superseded | `fe4b930d99`, then r28-F08. `authority-boundary-r28.test.ts` "a delayed resume cannot resurrect a runtime explicitly reclaimed…" |
| CX-r27-F15 | Primary reconnect publishes open before its fence | FIXED | `fe4b930d99`. `app/gateway/hooks/use-gateway-boot.test.tsx` "primary reconnect fences old work before publishing an open socket…" |
| CX-r27-F16 | An expired submit releases a successor's claim | FIXED, superseded | `fe4b930d99` (the token), then r28-F06/F07. `recovery-boundary.test.ts` "an expired submit cannot release the replacement submits lock" |
| CX-r27-F17 | Census and public API bypasses (extension, dynamic import, `src/test`, harness, `rawSet`) | FIXED, superseded | `fe4b930d99`, then r28-F18. `scripts/runtime-identity-census.test.ts` |
| CX-r27-F18 | A second cache instance keeps stale identity (P3) | FIXED, superseded | `fe4b930d99`, then r28-F17. `authority-boundary-r28.test.ts` "a second cache instance must not rewind…" |
| CX-r27-F19 | Detach then rebind loses lineage (P3) | FIXED | `fe4b930d99`. `authority-boundary-r28.test.ts` "detaching cannot erase evidence…" |
| CX-r28-F01 | The RPC dispatcher discards the runtime's recorded owner | FIXED | `44e26582a2`. `session/ownership-boundary-r29.test.ts` "an RPC uses the runtime owner…" |
| CX-r28-F02 | Preparatory awaits dispatch after a switch | FIXED | `44e26582a2`. `use-prompt-actions/pre-dispatch-r29.test.ts` ×2; `app/chat/tile-rewind-ownership.test.ts` "rewind must not send after its history read…"; `onboarding-handoff-owner.test.ts` "onboarding cannot create after its personalization await…" |
| CX-r28-F03 | Tile reload/restore/edit failures replace a newer turn | FIXED | `44e26582a2`. `tile-rewind-ownership.test.ts` "tile %s late failure leaves a newer running turn intact" ×3 |
| CX-r28-F04 | `turnStartedAt` is not a turn identity | FIXED | `44e26582a2`. `use-prompt-actions/turn-generation.test.ts` "a distinct backend turn invalidates sameTurn…" |
| CX-r28-F05 | A late `/compress` snapshot overwrites a later transcript | FIXED | `44e26582a2`. `turn-generation.test.ts` "compress completion respects newer-turn" |
| CX-r28-F06 | An expired submit's failure settles its successor | FIXED | `44e26582a2`. `use-prompt-actions/submit-lifetime.test.ts` "late submit failure cannot settle a successor…" |
| CX-r28-F07 | A late Stop acknowledgement clears a newer send's foreground busy | FIXED | `44e26582a2`. `submit-lifetime.test.ts` "a late Stop acknowledgement…" |
| CX-r28-F08 | Reclamation does not fence late submit/slash writes | FIXED | `44e26582a2`. `submit-lifetime.test.ts` "late submit failure does not recreate a reclaimed runtime"; `turn-generation.test.ts` "compress completion respects reclaimed" |
| CX-r28-F09 | Warm activation revives a turn that ended during its request | FIXED | `44e26582a2`. `use-session-actions/warm-activation-age.test.ts` |
| CX-r28-F10 | Owner keys collapse pool routes that share a `targetProfile` | FIXED | `44e26582a2`. `ownership-boundary-r29.test.ts` "different Desktop pool routes do not share a flight…" |
| CX-r28-F11 | Onboarding binds a remote receipt under the ambient owner | FIXED | `44e26582a2`. `onboarding-handoff-owner.test.ts` "receipt recovery binds under the receipt owner…" |
| CX-r28-F12 | The journal is owner-blind within one epoch | FIXED | `44e26582a2`. `inflight-turn-journal-owner.test.ts` ×2 |
| CX-r28-F13 | Epoch-less legacy journals bypass the gateway fence | FIXED | `44e26582a2`. `inflight-turn-journal-owner.test.ts` "an epoch-less legacy journal…" |
| CX-r28-F14 | A refused adoption re-homes the owner and deletes a binding | FIXED | `44e26582a2`. `ownership-boundary-r29.test.ts` "a refused adoption leaves…" |
| CX-r28-F15 | Submit admission is keyed only by stored id | FIXED | `44e26582a2`. `submit-lifetime.test.ts` "a pending submit for one owner does not block…" |
| CX-r28-F16 | Onboarding ignores the resume reply's continuation id | FIXED | `44e26582a2` (`handoff-leg.ts` uses `resumeReplyStoredId`). `onboarding-handoff-owner.test.ts` "a handoff resume uses and persists the backend continuation id". PATHS.md r29 and the commit message leave F16 out of their cause lists; this is a paperwork gap only |
| CX-r28-F17 | Registry resync does not repair the second cache's reverse map (P3) | FIXED | `44e26582a2`. `ownership-boundary-r29.test.ts` "registry resynchronization also repairs…" |
| CX-r28-F18a | The census misses owner-less call forms (`undefined`, spread, extracted, `.call`, computed) | FIXED, by a runtime guard rather than lint | `44e26582a2`. `session/owner-required.test.ts` (a runtime TypeError, and `@ts-expect-error`). Codex's 8 lint-form probes still fail by design |
| CX-r28-F18b | The census misses variable `import()`, `import.meta.glob`, `require` | OPEN: documented limit, needs Lake | See P8. The lint rule is deliberately not grown. No reviewer or Lake acceptance is on record yet |

**Reversed expectation (not a finding).** The r26 probe "a session create answered after a gateway switch binds nothing and closes the orphan" was deliberately reversed in `fe4b930d99`: no `session.close` is sent on the next gateway's ambient socket. Codex accepted this in r28. Repo test: `gateway-epoch-completions.test.tsx` "…sends no close to the next gateway".

#### Antigravity (22 findings, each a restatement of a same-round Codex finding)

| id | same as | outcome | evidence |
|---|---|---|---|
| AG-r20-F1 | CX-r20-F1/F2 | FIXED | `ee45735a89`, `submit-ownership.test.ts` |
| AG-r20-F2 | CX-r20-F4 | FIXED | `ee45735a89`, `submit-ownership.test.ts` |
| AG-r20-F3 | CX-r20-F3 | FIXED | `ee45735a89`, `submit-ownership.test.ts` |
| AG-r21-F1 | CX-r21-F1 | FIXED, superseded | `14fda1ed22`, then `59e0b56d62`. `runtime-lineage.test.ts` |
| AG-r21-F2 | CX-r21-F2 | FIXED | `14fda1ed22`, `runtime-lineage.test.ts` warm/cold |
| AG-r21-F3 | CX-r21-F3 | FIXED, superseded | `14fda1ed22`, then `59e0b56d62` |
| AG-r21-F4 | onboarding pre-adoption write | FIXED | `14fda1ed22`, then `59e0b56d62`. `runtime-lineage.test.ts` "(the onboarding shape)", `onboarding-handoff-ownership.test.ts` |
| AG-r22-B1 | CX-r22-F1 | FIXED, superseded | `59e0b56d62`, then `d224fe593b` |
| AG-r22-B2 | CX-r22-F2 | FIXED | `59e0b56d62` |
| AG-r22-B3 | CX-r22-F3 | FIXED | `59e0b56d62`, `onboarding-handoff-ownership.test.ts` |
| AG-r23-F1 | CX-r23-F1 | FIXED | `d224fe593b`, `resume-ordering.test.ts` |
| AG-r24-F1 | CX-r24-F1 | FIXED, superseded | `6a483bc60f`, then r27-F02, then r28-F02 |
| AG-r24-F2 | CX-r24-F2 | FIXED, superseded | `6a483bc60f`, then r26-F6 |
| AG-r24-F3 | CX-r24-F3 | FIXED, superseded | `6a483bc60f`, then r26-F3/F4, then r27-F15 |
| AG-r24-F4 | CX-r24-F4 | FIXED | `6a483bc60f`, `gateway-epoch.test.ts` |
| AG-r26-F1 | CX-r26-F1 | FIXED, superseded | as CX-r26-F1 |
| AG-r26-F2 | CX-r26-F2 | FIXED | `1e8a6aa20b`, `runtime-boundary.test.ts` |
| AG-r26-F3 | CX-r26-F3 | FIXED | `1e8a6aa20b` |
| AG-r26-F4 | CX-r26-F4 | FIXED | `1e8a6aa20b`, `gateway-epoch-completions.test.tsx` |
| AG-r26-F5 | CX-r26-F5 | FIXED, superseded | as CX-r26-F5 |
| AG-r26-F6 | CX-r26-F6 | FIXED, superseded | as CX-r26-F6 |
| AG-r26-F7 | CX-r26-F7 | FIXED, superseded | as CX-r26-F7 |

**Counts, rounds 19–28:**

| Reviewer | findings | FIXED | OPEN | DISPROVED | NEEDS LAKE |
|---|---|---|---|---|---|
| Codex | 65 | 64 (F18 counted as fixed in its call-form part, F18a) | 1: F18b, a documented limit | 0 | 1 (F18b) |
| Antigravity | 22 | 22 | 0 | 0 | 0 |

F18b is counted twice, as OPEN and as NEEDS LAKE: it is open, and only Lake can accept the limit.

**The status of round 29 itself.** Every r28 FIXED row rests on the implementer's red-first run: 29 of 31 new tests fail on `4ba5f4131e`, and the 2 that pass are controls. Round 29 is now under review.

### Totals, rounds 1–28

| Source | rows | FIXED | OPEN | DISPROVED | NEEDS LAKE |
|---|---|---|---|---|---|
| Round-0 track items | 16 | 15 | 0 | 0 | 1 |
| Codex r1–r18 | 69 | 57 | 6 | 1 | 5 |
| Antigravity r1–r18 | 11 | 10 | 1 (provenance only) | 0 | 0 |
| Codex r19–r28 | 65 | 64 | 1 (F18b) | 0 | 1 (F18b, also counted OPEN) |
| Antigravity r19–r28 | 22 | 22 | 0 | 0 | 0 |
| **Total** | **183** | **168** | **8** | **1** | **7** |

**Notes on the totals:**
- F18b is counted in both OPEN and NEEDS LAKE, so the outcome columns sum to 184 across 183 rows.
- Several FIXED rows are "superseded": a later round reopened the area and the final fix is named. Every chain ends at a test passing at `44e26582a2`.
- **Round 29 (`44e26582a2`) is under review.** Every FIXED row whose final fix is in round 29 rests on the implementer's red-first run (`.t3-fix-r29-20260928/red-on-4ba5f4131e.log`) until the r29 reviews report.
- **Where the OPEN rows point:**
  - CX-r9-N, CX-r11-R6cd, CX-r12-HY, CX-r17-N and CX-r18-N belong to the combined-release gate.
  - CX-r13-#7 belongs to test hygiene.
  - AG-r3–6/r8 is a provenance gap only.
  - CX-r28-F18b is P8's documented limit.

**Paperwork corrections found while compiling:**
- `PATHS.md` round 29 and the `44e26582a2` commit message omit CX-r28-F16, although its fix and test landed.
- The `pre-dispatch.test.ts` title "R27 F2" covers CX-r27-F10.
- DISPOSITIONS §1/3/5 wording is stale (see "Needs Lake" item 7).
