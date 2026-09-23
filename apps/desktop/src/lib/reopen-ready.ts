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
// (null). The payload carries the resolved target identity so main can check
// the window came up on the session it was asked to open.

export type BootFunctionalState = 'pending' | 'ready' | { error: string }

export interface ReopenReadyInputs {
  boot: BootFunctionalState
  // The window's intended target session (from its launch URL), or null.
  target: null | string
  // $selectedStoredSessionId / $activeSessionId (runtime) / $resumeExhaustedSessionId
  selected: null | string
  runtime: null | string
  exhausted: null | string
  profile: null | string
}

export interface ReopenReadyPayload {
  ok: boolean
  phase: 'boot' | 'session'
  profile: null | string
  sessionId: null | string
  error?: string
}

export function reopenReadyVerdict(inputs: ReopenReadyInputs): null | ReopenReadyPayload {
  const { boot, target, selected, runtime, exhausted, profile } = inputs

  if (boot === 'pending') {
    return null
  }

  if (typeof boot === 'object') {
    return { ok: false, phase: 'boot', profile, sessionId: target, error: boot.error }
  }

  if (!target) {
    return { ok: true, phase: 'boot', profile, sessionId: null }
  }

  if (exhausted === target) {
    return { ok: false, phase: 'session', profile, sessionId: target, error: `session ${target} failed to load` }
  }

  if (selected === target && runtime) {
    return { ok: true, phase: 'session', profile, sessionId: target }
  }

  return null
}
