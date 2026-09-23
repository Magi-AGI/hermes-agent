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

// Keeps main's record of this window CURRENT (Codex A4 round 3 #1). The window
// used to report once, for its launch session; after /new moved it to another
// session, Reopen compared the saved new session with that stale launch verdict
// and destroyed a healthy window. Now the target is the window's current
// session, and every change of verdict — including back to pending while a new
// session, route or boot attempt is being established — is sent; an unchanged
// one is not re-sent. Every report is stamped with its attempt, so main never
// pairs a verdict (or a health result) from an earlier attempt with the latest.
export function createReopenReadySignaller(send: (payload: ReopenReadyPayload) => void) {
  let last: null | string = null
  let attempt = 0
  let wasPending = false

  return (inputs: ReopenReadyInputs) => {
    const verdict = reopenReadyVerdict(inputs)

    if (!verdict && !wasPending) {
      attempt += 1
    }

    wasPending = !verdict

    const payload: ReopenReadyPayload = {
      ...(verdict ?? {
        ok: false,
        pending: true as const,
        phase: 'pending' as const,
        profile: inputs.profile,
        connectionId: inputs.connectionId,
        sessionId: inputs.target
      }),
      attempt
    }

    const key = JSON.stringify(payload)

    if (key !== last) {
      last = key
      send(payload)
    }
  }
}
