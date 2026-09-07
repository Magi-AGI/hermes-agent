# Design: sequential functional-readiness gating for session-window reopen

**Status:** DRAFT — for Codex + Antigravity review before implementation.
**Author:** Claude (Sonnet 5), session `session_01HzRzrDzhEYsF1dUujeMi6H`, 2026-09-07.

## 0. Conclusion of the previous triad run (context)

For context on what preceded this: the prior triad pass reconciled the fork onto
upstream `9a84bee265` (4 buckets: code-0/RelayBuffer, STT/web_server lifespan,
compaction route guard, desktop session windows), then added two hardening
items — a peer-instance-window resume fix
(`use-desktop-integrations.ts`, commit `82972e687a`) and a structural anti-bloat
fix for the skill-curator self-improvement loop (`background_review.py` +
`curator.py` + guards, commit `48bb63b8d6`). That work went through two review
rounds: round 1 Codex returned NO-GO with 2 blocking findings + 3 amend items
(Antigravity's independent audit found the same issues); round 2, after fixes,
both reviewers gave GO. All 6 commits were pushed to `magi/reconcile-2026-09-06`
and deployed to the live install same day. **That work is closed — this new
design is unrelated to it** (see §1.1 for why, since the user's first question
was whether this is a regression from that pass).

## 1. Problem statement

Across roughly the last dozen hardening passes, variations of "opening/reopening
multiple session windows breaks something" have kept resurfacing as symptoms —
`ready_send_failed` storms, peer-window resume clobbering state, and now (this
session) a reproducible full-process freeze in the per-profile backend
(`hermes_cli.main --profile <p> serve`) triggered by session-window reconnects.
Each pass has fixed the *specific trigger* found that time. The class of symptom
has not gone away, because the underlying assumption — that pacing window opens
by a shallow "did this window's transport connect" signal is enough to protect
a shared, single-process-per-profile backend — was never actually correct, and
no fix so far has addressed that assumption directly.

**Decision this design proposes:** stop patching individual triggers and change
the invariant instead — a new session window must not be spawned until the
previous one is confirmed *functional*, not merely *connected*, and the queue
must never silently give up and proceed past an unhealthy backend.

### 1.1 Live evidence gathered this session (2026-09-07)

- `claudetriad` profile's `state.db` is **5.37 GB** vs. 0.24 MB for every
  sibling profile (default/wikiadmin/wikieditor/wikireader) — by far the
  heaviest-used profile, from months of accumulated session/message history.
- Reproduced the freeze 4 times in one session: twice after a targeted
  backend-process restart, once after a *full* Hermes desktop app restart —
  each time recurring within 3-4 minutes of reopening as few as ~10 windows.
- Two `py-spy` thread dumps of the frozen `claudetriad serve` process, taken
  5 seconds apart, were **byte-for-byte identical across all ~46 OS threads**
  (including the asyncio `ProactorEventLoop` main thread, which should cycle
  every ~60ms under normal operation) — this is a genuine full-process freeze,
  not merely a dead accept-loop coroutine with everything else still running.
- Ruled out system-wide resource contention as the cause: overall CPU was
  ~13%, 24 GB RAM free, at the moment of freeze.
- `hermes_cli/web_server.py`'s `_run_serve()` docstring already documents a
  related known issue: Windows' default `ProactorEventLoop` can make a
  uvicorn-bound socket "never accept" (referenced as upstream **#50641**),
  with an existing custom-runner workaround — plausibly the same bug class
  resurfacing under a trigger the workaround doesn't fully cover.
- Traced the exact gap in the *existing* mitigation (the "reopen queue",
  re-ported this reconcile pass in Bucket 4, `d904f897e3`): see §2.

## 2. The existing mitigation and its actual gap

`apps/desktop/electron/main.ts` already has a queue built specifically to
prevent "a burst that overwhelms the single-process backend" (see the comment
on `reportPrimaryGatewayState` in `apps/desktop/src/store/gateway.ts`):

```ts
// main.ts ~13705
async function reopenSessionWindows() {
  for (const entry of entries) {
    const win = createSessionWindow(entry.sessionId, { ... })
    if (win && !win.isDestroyed()) {
      await waitForSessionWindowGatewayReady(win)   // <-- the gate
      await new Promise(resolve => setTimeout(resolve, REOPEN_WINDOW_SETTLE_MS)) // 400ms
    }
  }
}
```

`waitForSessionWindowGatewayReady` (main.ts ~13658) resolves on **either**:
1. an IPC signal (`hermes:window:gateway-ready`) fired the moment the window's
   gateway WebSocket transport reaches `state === 'open'` (renderer side:
   `signalSessionGatewayReady()` in `store/gateway.ts`, wired in
   `preload.ts:418`), **or**
2. a **20-second timeout**, whichever comes first — and proceeds to open the
   next window regardless of which one fired.

Two problems:

- **(a) "Open" ≠ "functional".** The renderer's own boot sequence
  (`apps/desktop/src/app/gateway/hooks/use-gateway-boot.ts` ~625-660) treats
  the post-connect profile/session load as best-effort and explicitly
  **non-blocking**: `void refreshActiveProfile().catch(() => undefined)` is
  fired but not awaited, with the comment "switch completion must not wait out
  backoff timers against an unhealthy backend." That's the right call for a
  *single window's own responsiveness* — but the reopen queue currently
  advances on the exact same `'open'` event as this fire-and-forget call, so
  window N+1 is created while window N's `refreshProfiles()` (with its own
  2-retry/500ms-1000ms backoff chain in `store/profile.ts:90-120`) may still
  be in flight, retrying, or about to hit the 60s hard timeout we've observed
  in every log this session (`[profiles] refreshProfiles failed after 3
  attempt(s): ... Timed out connecting to Hermes backend after 60000ms`).
- **(b) The timeout doesn't fail safe, it fails open.** Under the one
  condition the queue exists to protect against — the shared backend already
  struggling — the 20s wait is the path most likely to be hit, and its
  behavior is to give up and spawn *another* window against the already
  struggling backend anyway. The mitigation degrades to a no-op exactly when
  it's needed most.

## 3. Proposed design

### 3.1 A distinct "functionally ready" signal, separate from "gateway open"

Add a new completion signal that resolves only after the boot-critical calls a
window makes right after connecting have *settled* (resolved or definitively
failed after their own retries) — not merely on WS transport open. Candidate
calls to gate on, based on the boot sequence already in `use-gateway-boot.ts`:
`adoptPrimaryProfile`, `refreshActiveProfile`/`refreshProfiles`,
`refreshHermesConfig`, `refreshSessions`.

This must be a **new** signal, not a repurposing of the existing fire-and-forget
one — a normal single-window open should keep its current non-blocking UX
(don't make opening one window feel slower to fix a multi-window queueing
problem). The queue is the only consumer that should await it.

Open question for review: should "functionally ready" mean *all* of those
calls succeeded, or is it acceptable for some (e.g. `refreshHermesConfig`) to
fail non-fatally while `refreshProfiles`/`refreshSessions` succeeding is the
hard requirement? We lean toward the latter — profiles/sessions are what the
window needs to be usable — but want Codex/Antigravity's read on this.

### 3.2 Gate on backend health directly, not only on window self-report

Before creating window N+1, poll the shared per-profile backend's existing
`/api/status` endpoint (see `hermes_cli/web_server.py`) with a short timeout
(proposal: 2-3s) and require a fast, successful response. This decouples "is
the shared resource actually healthy" from "did one particular window happen
to report in" — directly answering the case this session surfaced, where the
backend event loop itself was frozen (not just one window's own request
racing a slow response).

### 3.3 Replace the silent timeout-bypass with backoff + explicit failure surfacing

Remove the "wait 20s, then proceed regardless" behavior. Replace with:
- Retry the health check / functional-ready wait with backoff (exact schedule
  TBD — proposal: a few attempts over ~30-60s total, matching the existing
  `refreshProfiles` backoff philosophy already in the codebase).
- If the backend still isn't healthy after that budget, **stop the reopen
  sequence** rather than continuing to spawn windows against it, and surface a
  clear message: "Restored N of M windows — Hermes backend isn't responding,
  stopped to avoid overloading it" (exact UX TBD — could be a banner, a
  notification, or a dialog; open question for review).
- The already-open windows should not be torn down — only the *remaining*
  reopen queue halts.

## 4. Scope / blast radius

Files expected to change:
- `apps/desktop/electron/main.ts` — `reopenSessionWindows()` /
  `waitForSessionWindowGatewayReady()` (replace/extend the gate).
- `apps/desktop/electron/preload.ts` — new IPC channel for the
  "functionally ready" signal (in addition to, not replacing,
  `hermes:window:gateway-ready`).
- `apps/desktop/src/app/gateway/hooks/use-gateway-boot.ts` — emit the new
  signal once the boot-critical calls settle, without changing their
  existing fire-and-forget behavior for the single-window case.
- `apps/desktop/src/store/gateway.ts` — possibly, if the signal is easier to
  centralize there alongside the existing `reportPrimaryGatewayState`.
- New: a backend health-check helper (main-process side, Node `fetch` against
  `/api/status`) — location TBD, likely near where backend ports/URLs are
  already tracked in `main.ts`.
- Tests: `apps/desktop/electron/session-windows.test.ts` already has coverage
  for the reopen path's profile-retention; needs new cases for (a) queue
  stalling correctly when the backend is slow/unresponsive, (b) queue
  proceeding once it recovers, (c) the abort-and-report path when the backend
  never recovers within budget, (d) confirming normal single-window open
  latency is unaffected.

## 5. Explicit non-goals for this pass

- Not attempting to fix the underlying per-profile backend freeze itself
  (the `state.db` size / possible Windows ProactorEventLoop accept-socket
  issue). That's a separate, deeper investigation (py-spy dumps saved at
  `C:\Users\Lake\AppData\Local\Temp\pyspy-dump{1,2}.txt` on the live machine
  for reference). This design is about the *desktop app's* behavior when the
  backend is unhealthy for *any* reason, not about preventing the backend
  from becoming unhealthy in the first place.
- Not changing behavior for windows opened via the normal (non-reopen-saved)
  path — scope is intentionally limited to `reopenSessionWindows()` first,
  to limit blast radius, unless review concludes the same gate should apply
  more broadly (e.g. to `Ctrl+Shift+N`/pop-outs opened in quick succession).

## 6. Questions for Codex / Antigravity — ORIGINAL (superseded by §7)

1. Is gating on "all boot-critical calls settled" vs. "profiles+sessions
   specifically" the right bar for "functional"? (§3.1)
2. Backoff schedule and total budget before giving up (§3.3) — we don't have
   a strong opinion yet; what's defensible without making a real backend
   outage feel like a hang?
3. UX for the abort-and-report case — banner vs. notification vs. dialog,
   and whether the user should get a manual "retry remaining windows" action.
4. Should the `/api/status` health check apply per-profile (only the profile
   of the window about to open) or check the specific backend process the
   next window would target? (Multiple profiles have independent backend
   processes — a struggling `claudetriad` backend shouldn't block reopening
   `default`-profile windows, and vice versa.)
5. Any concern about false-positive stalls — e.g. a legitimately large
   session's data load taking a while without the backend actually being
   unhealthy — and how to distinguish "slow" from "frozen" in the health
   check design.

## 7. Review round 1 — synthesis of Codex + Antigravity, and what's still open

Both reviewers independently confirmed the §1/§2 diagnosis and gave
architectural GO. Both also independently flagged the exact same file/line
(`main.ts:13658`'s premature release + `main.ts:13691`'s fail-open timeout).
Reconciling their answers to the 5 questions surfaced two places where
Codex's design is materially more correct than Antigravity's (verified
directly against source below, not just taken on claim), one place where
Codex found a problem Antigravity's answer doesn't address, one genuinely new
finding from Antigravity that needs Codex's confirmation, and one numeric
disagreement still needing a final call.

### 7.1 Adopted as-is (Codex correction over Antigravity's answer — verified)

**Health check must reuse `backend-health.ts`'s `waitForHermesReady()`, not a
raw `/api/status` fetch.** Antigravity's Q2/Q4 answers both assume a direct
`fetch('http://127.0.0.1:<port>/api/status')`. Codex caught that this
duplicates an existing, already-tested abstraction
(`apps/desktop/electron/backend-health.ts:231`, wrapped in
`main.ts:6542`) that already prefers `/api/health` and only falls back to
`/api/status` for old backends, and already handles auth-failure cases
(`backend-health.test.ts:68,184` cover this). Verified directly: that
function exists exactly as described. **Amendment adopted as Codex specified
— reuse `waitForHermesReady()`, do not write a new raw poll.**

**Functional-ready must be a result signal (`{ ok, phase, profile, sessionId,
error }`), not merely "boot sequence finished running."** Antigravity's Q1
answer proposes firing the ready signal right after `completeDesktopBoot()`,
regardless of whether the awaited calls inside actually succeeded. Verified
directly against `use-gateway-boot.ts` (~1053-1065): `refreshSessions()`'s
failure is explicitly caught and treated as non-fatal to boot — the comment
reads "Session-list population is never boot-fatal... must leave the app
usable with an empty sidebar." A window can reach `completeDesktopBoot()`
with an empty, failed session list, and Antigravity's design would report
that window as "functionally ready" anyway. **Amendment adopted as Codex
specified** — the reopen queue must only advance on `ok: true` for the
required phases (gateway open, intended profile adopted, boot-fatal config
loaded, session list/route settled), not on boot merely having run to
completion.

### 7.2 Adopted as-is (Codex found a gap Antigravity's answer doesn't address)

**Health-check scoping needs a routing-correctness decision, not just a
profile lookup.** Antigravity's Q4 answer (`backendPool.get(entry.profile ||
'default')`) assumes profile is always sufficient to find the right backend.
Codex's amendment #3 found an explicit existing warning against that
assumption in `apps/desktop/src/store/session.ts:187` (profile-only routing
can hit the wrong backend), tied to the `WindowConnectionRouteRegistry` model
in `window-connection-route.ts`. Antigravity's answer doesn't engage with
this at all. **Decision needed (not yet made): either (a) explicitly scope v1
to profile-only restored windows with this documented as a known limitation,
or (b) thread `{ connectionId, profile, registryScoped }` through the saved
entry/open path and reuse `WindowConnectionRouteRegistry`.** Leaning toward
(a) to keep this pass's blast radius bounded — the reopen-saved-windows
surface is the one under discussion, and (b) is a larger, separable piece of
work — but this should be confirmed by both reviewers before implementation,
not decided unilaterally here.

### 7.6 RESOLVED — both reviewers signed off on all items (round 2)

- **7.1 (health probe reuse, structured signal shape):** agreed by both, no
  changes.
- **7.2 (connection-routing scope):** **Option (a) adopted** — v1 scoped to
  profile-only restored windows, documented as a known limitation. Both
  reviewers independently verified `persistSessionWindowsState()`
  (`main.ts:2988-3000`) only ever serializes `sessionId`/geometry/`profile` —
  there is no `connectionId` in the persisted surface today, so profile-only
  matches what actually exists on disk; threading
  `WindowConnectionRouteRegistry` through is out of scope for this pass.
- **7.3 (`isLoading()` race):** **confirmed by both**, independently, via
  different methods — Codex traced `loadURL()`'s actual implementation
  (`main.ts:1776`) to show it fires-and-forgets a navigation with no
  synchronous guarantee `isLoading()` reads true by the time the queue checks
  it; Antigravity traced the Chromium `did-start-loading` lifecycle timing to
  the same conclusion. **Fix adopted:** the window-open factory
  (`createSessionWindow` / the registry in `session-windows.ts:141`) must
  return whether it created a new window or returned an existing live one
  (`{ win, created }` — naming per Codex). `waitForSessionWindowGatewayReady`
  must only take the immediate-resolve fast-path when `created === false`
  (an existing window that "reopen" merely focused). For `created === true`,
  never check `isLoading()` on entry — always wait for the structured
  functional-ready signal, the health-check failure path, `did-fail-load`, or
  the backoff-budget timeout.
- **7.4 (backoff budget):** **Codex's ~45s budget adopted**, jittered
  750ms-1s polling via `waitForHermesReady()`. Both reviewers agree a
  12-15s budget would false-abort against the 5.37GB `claudetriad` profile's
  legitimate cold-boot I/O (large session history reads, WAL flushes).
  **One implementation detail Codex flagged that still needs a call during
  implementation** (not a disagreement, just unresolved): should the
  functional-ready signal's own internal wait be budgeted consistently with
  the existing `STARTUP_REQUEST_TIMEOUT_MS` (60s) constant already used
  elsewhere in the codebase (`apps/desktop/src/api/client.ts`), or should
  readiness instead fire as soon as the target session specifically becomes
  renderable (independent of that constant)? Leave as an implementation-time
  decision, not a blocker to starting.

**Design is now triad-approved (GO, no open items) and ready for
implementation.**

### 7.3 New finding requiring Codex's confirmation (not in Codex's original review) — RESOLVED, see §7.6

Antigravity raised a specific claim Codex's review didn't cover: that
`main.ts:13663`'s fast-path check —
```ts
if (win.isDestroyed() || !win.webContents.isLoading()) { resolve(); return }
```
— intended only to skip the wait for an *already-open* window that "reopen"
merely focused, can spuriously fire for a **brand-new** window too, if
`win.webContents.isLoading()` momentarily reads `false` in the gap between
`BrowserWindow` construction and Chromium actually starting navigation. If
true, this would mean the *existing* queue can already be bypassed entirely
under the right timing (not just degraded via the 20s timeout) — a more
severe version of the bug than either review's diagnosis in §2 describes.
This has NOT been independently verified against Electron's actual behavior
(unlike 7.1/7.2, which were confirmed directly in source above) — **flagging
for Codex to confirm or refute before it's treated as a real finding**, and
if confirmed, the fix (per Antigravity: only use the `isLoading()` fast-path
for windows that were already open and merely focused, not newly spawned
ones) should fold into the same implementation pass.

### 7.4 Open numeric disagreement — needs Codex's final call

Backoff/budget: Codex proposes ~2s probe timeout, 750ms-1s polling with
jitter, 30-45s total budget before stopping. Antigravity proposes 3 fixed
probes (2.5s timeout each, 1.0s then 2.5s backoff) totaling ~12-15s. These
aren't reconcilable by splitting the difference — they reflect a real
trade-off (Antigravity's own Q5 answer acknowledges a large session DB can
make functional-ready legitimately slow while the backend stays healthy,
which argues for Codex's longer budget; but a shorter budget fails faster
when the backend really is wedged). Per the established rule that Codex is
authoritative when the two disagree, defaulting to **Codex's 30-45s budget
with jittered 750ms-1s polling** unless Codex revises this in light of
Antigravity's shorter-budget rationale — but calling this out explicitly
rather than silently picking a side.

### 7.5 Not in conflict, adopted directly

- Functional-readiness required phases (Codex's list: gateway open, intended
  profile adopted, boot-fatal config loaded, session list/route settled
  enough to render) — Antigravity's answer is compatible once corrected per
  §7.1 to require actual success (`ok`) on those phases rather than "settled
  either way."
- Non-modal abort UX with restored-N/M messaging and a resume action — both
  reviewers agree; adopting Antigravity's more concrete mechanics
  (`hermes:window:reopen-halted` IPC payload shape, sticky banner, explicit
  "do NOT use `dialog.showMessageBoxSync`" caution) as the starting spec.
- Per-profile isolation (a wedged `claudetriad` backend must not block
  reopening healthy `default`-profile windows) — both agree; this is
  actually the same underlying decision as §7.2, resolved together with it.
- Distinguishing slow-but-healthy from frozen via the health probe succeeding
  fast even when functional-ready is delayed — both agree; this is the
  mechanism `waitForHermesReady()` (§7.1) already provides.
