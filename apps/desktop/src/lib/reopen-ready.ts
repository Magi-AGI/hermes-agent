// "Functionally ready" for the saved-window reopen queue (Magi, sync A4; Codex
// review #3). The queue opens one window at a time and waits for this verdict
// before opening the next, so it must mean the window can actually be used:
//
//   - every REQUIRED boot operation succeeded (gateway connect, profile
//     adoption, the mandatory config fetch) — `boot: 'ready'`
//   - AND the window's intended target session (the session in its launch URL)
//     is selected and bound to a live runtime.
//
// A window with no target session (a restored peer that was showing a fresh
// chat) is ready as soon as boot is. A terminal boot failure, or the target's
// resume being exhausted, is reported ok:false. Anything else is still pending
// (null). The payload carries the window's COMPLETE identity — the session and
// its fully resolved owner route (profile AND connection), resolved before
// sending — so main checks and probes exactly what the window is on, and route
// and readiness can never arrive separately (Codex A4 round 5).

export type BootFunctionalState = 'pending' | 'ready' | { error: string }

export interface ReopenReadyInputs {
  boot: BootFunctionalState
  // The window's intended target session (from its launch URL), or null.
  target: null | string
  // $selectedStoredSessionId / $activeSessionId (runtime) / $resumeExhaustedSessionId
  selected: null | string
  runtime: null | string
  exhausted: null | string
  // The target session's resolved OWNER route (windowSessionReport).
  profile: null | string
  connectionId: null | string
}

export interface ReopenReadyPayload {
  ok: boolean
  // Which establishment attempt this report belongs to (Codex A4 round 4 #1):
  // a new one starts each time the window goes (back) to pending — a new
  // session, route or boot attempt. Stamped by createReopenReadySignaller.
  attempt?: number
  pending?: true
  phase: 'boot' | 'pending' | 'session'
  profile: null | string
  connectionId: null | string
  sessionId: null | string
  error?: string
}

export function reopenReadyVerdict(inputs: ReopenReadyInputs): null | ReopenReadyPayload {
  const { boot, target, selected, runtime, exhausted, profile, connectionId } = inputs

  if (boot === 'pending') {
    return null
  }

  if (typeof boot === 'object') {
    return { ok: false, phase: 'boot', profile, connectionId, sessionId: target, error: boot.error }
  }

  if (!target) {
    return { ok: true, phase: 'boot', profile, connectionId, sessionId: null }
  }

  if (exhausted === target) {
    return {
      ok: false,
      phase: 'session',
      profile,
      connectionId,
      sessionId: target,
      error: `session ${target} failed to load`
    }
  }

  if (selected === target && runtime) {
    return { ok: true, phase: 'session', profile, connectionId, sessionId: target }
  }

  return null
}

// The window's establishment state: everything that decides WHICH session on
// WHICH route it is establishing. `ambient` is the window's active route (a
// change of it is an establishment change); the session's resolved OWNER route
// is supplied separately, to settle().
export type ReopenReadyState = Omit<ReopenReadyInputs, 'connectionId' | 'profile'> & {
  ambient: { connectionId: null | string; profile: null | string }
}

// A verdict waiting for its owner route (see createReopenReadySignaller).
export interface ReopenReadyStep {
  readonly attempt: number
  readonly verdict: ReopenReadyPayload
}

// Keeps main's record of this window CURRENT (Codex A4 rounds 3-6).
//
// update() runs synchronously on every state change. On ANY establishment
// change (target session, boot state, ambient route) it starts a new attempt
// and sends "pending" IMMEDIATELY — before any async work — so main never goes
// on holding the previous session's ready verdict while the new one resolves
// (round 6 #2). A pending report carries no route: an unresolved window never
// authorizes a probe against a fallback/ambient route.
//
// Once the verdict is ready (or failed), update() returns a step; the caller
// resolves the session's owner route and calls settle(step, route), which
// sends the verdict WITH that route — unless the step was superseded meanwhile.
// Unchanged reports are not re-sent.
export function createReopenReadySignaller(send: (payload: ReopenReadyPayload) => void) {
  let lastSent: null | string = null
  let attempt = 0
  let establishment: null | string = null
  let step: (ReopenReadyStep & { key: string; settled: boolean }) | null = null

  const emit = (payload: ReopenReadyPayload) => {
    const key = JSON.stringify(payload)

    if (key !== lastSent) {
      lastSent = key
      send(payload)
    }
  }

  const pending = (target: null | string): ReopenReadyPayload => ({
    ok: false,
    pending: true,
    phase: 'pending',
    profile: null,
    connectionId: null,
    sessionId: target,
    attempt
  })

  return {
    update(state: ReopenReadyState): null | ReopenReadyStep {
      const bootKey = typeof state.boot === 'object' ? `error:${state.boot.error}` : state.boot
      const nextEstablishment = JSON.stringify([state.target, bootKey, state.ambient.profile, state.ambient.connectionId])

      if (nextEstablishment !== establishment) {
        establishment = nextEstablishment
        attempt += 1
        step = null
        emit(pending(state.target))
      }

      const verdict = reopenReadyVerdict({ ...state, profile: null, connectionId: null })

      if (!verdict) {
        step = null
        emit(pending(state.target))

        return null
      }

      const key = JSON.stringify(verdict)

      if (step && step.key === key) {
        // Same verdict: re-resolve only if it has not been sent yet (a
        // resolution can be cancelled by a re-render).
        return step.settled ? null : step
      }

      step = { attempt, verdict, key, settled: false }

      return step
    },
    settle(settling: ReopenReadyStep, route: { connectionId: null | string; profile: null | string }) {
      if (settling !== step || step.settled) {
        return
      }

      step.settled = true
      emit({ ...settling.verdict, profile: route.profile, connectionId: route.connectionId, attempt: settling.attempt })
    }
  }
}
