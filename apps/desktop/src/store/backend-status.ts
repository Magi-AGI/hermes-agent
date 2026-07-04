import { atom } from 'nanostores'

// Renderer-side model of a backend's health (Task 9a). Distinct from the global
// $desktopBoot/setup state: a per-SESSION backend failing must be attributable to
// that session, NOT surfaced as a global "Gateway needs setup" for the whole app.
// Actual restart/reap controls need the Task 10 IPC APIs; this slice is the model
// + current-window wiring only.
export type BackendScope = 'primary' | 'profile' | 'session'
export type BackendState = 'ready' | 'starting' | 'unresponsive' | 'failed' | 'restarting'

export interface BackendScopeRef {
  scope: BackendScope
  profile: string | null
  sessionId?: string | null
}

export interface BackendStatus extends BackendScopeRef {
  state: BackendState
  message?: string
  lastError?: string
  updatedAt: number
}

// Deterministic identity for a backend, mirroring the Electron pool keying
// (`profile:<profile>` / `session:<profile>:<sessionId>`) so renderer status and
// main-process backends line up 1:1. Session scope REQUIRES a sessionId — never
// silently collapse a session status onto the profile key.
export function backendScopeKey({ scope, profile, sessionId }: BackendScopeRef): string {
  const profileKey = (profile ?? '').trim() || 'default'

  if (scope === 'session') {
    const sid = (sessionId ?? '').toString().trim()

    if (!sid) {
      throw new Error('session scope requires a sessionId')
    }

    return `session:${profileKey}:${sid}`
  }

  if (scope === 'profile') {
    return `profile:${profileKey}`
  }

  return 'primary'
}

// Statuses keyed by backendScopeKey. A plain object (not a Map) so nanostores
// change-detection is a cheap reference swap and React subscribers re-render.
export const $backendStatuses = atom<Record<string, BackendStatus>>({})

function nowMs(): number {
  return Date.now()
}

export function setBackendStatus(
  ref: BackendScopeRef,
  state: BackendState,
  extra: { message?: string; lastError?: string } = {}
): string {
  const key = backendScopeKey(ref)

  const status: BackendStatus = {
    scope: ref.scope,
    profile: ref.profile ?? null,
    ...(ref.scope === 'session' ? { sessionId: (ref.sessionId ?? '').toString() } : {}),
    state,
    ...(extra.message ? { message: extra.message } : {}),
    ...(extra.lastError ? { lastError: extra.lastError } : {}),
    updatedAt: nowMs()
  }

  $backendStatuses.set({ ...$backendStatuses.get(), [key]: status })

  return key
}

export function getBackendStatus(key: string): BackendStatus | undefined {
  return $backendStatuses.get()[key]
}

export function clearBackendStatus(key: string): void {
  const next = { ...$backendStatuses.get() }

  delete next[key]
  $backendStatuses.set(next)
}

// A conservative scope to use BEFORE main has resolved the real backend (e.g. the
// initial 'starting' state, or a failure before getConnection returns). It must
// NEVER invent a session scope from URL intent: under default `profile`
// isolation an existing-session pop-out is profile-backed. A named-profile
// pop-out is profile-scoped (that backend exists); the primary window is
// primary-scoped. Once a connection resolves, callers switch to
// conn.backendScope (the authoritative, main-resolved scope).
export function conservativeWindowBackendScope(profile: string | null): BackendScopeRef {
  const p = (profile ?? '').trim()
  return p ? { scope: 'profile', profile: p } : { scope: 'primary', profile: null }
}
