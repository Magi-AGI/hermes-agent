import { beforeEach, describe, expect, it } from 'vitest'

import {
  $backendStatuses,
  backendScopeKey,
  clearBackendStatus,
  conservativeWindowBackendScope,
  getBackendStatus,
  setBackendStatus
} from './backend-status'

beforeEach(() => {
  $backendStatuses.set({})
})

describe('backendScopeKey', () => {
  it('keys primary / profile / session deterministically (mirrors the pool)', () => {
    expect(backendScopeKey({ scope: 'primary', profile: null })).toBe('primary')
    expect(backendScopeKey({ scope: 'profile', profile: 'claudetriad' })).toBe('profile:claudetriad')
    expect(backendScopeKey({ scope: 'profile', profile: null })).toBe('profile:default')
    expect(backendScopeKey({ scope: 'session', profile: 'wikireader', sessionId: 'session-1' })).toBe(
      'session:wikireader:session-1'
    )
  })

  it('a session scope key includes BOTH the profile and the stored session id', () => {
    const key = backendScopeKey({ scope: 'session', profile: 'wikireader', sessionId: 'stored-7' })
    expect(key).toContain('wikireader')
    expect(key).toContain('stored-7')
    // Different sessions under the same profile never collide.
    expect(key).not.toBe(backendScopeKey({ scope: 'session', profile: 'wikireader', sessionId: 'stored-8' }))
  })

  it('session scope with a missing sessionId throws (never collapses onto the profile)', () => {
    expect(() => backendScopeKey({ scope: 'session', profile: 'wikireader' })).toThrow(/sessionId/)
    expect(() => backendScopeKey({ scope: 'session', profile: 'wikireader', sessionId: '' })).toThrow(/sessionId/)
  })
})

describe('conservativeWindowBackendScope', () => {
  it('a named profile → profile scope (NEVER session, since URL intent is not proof)', () => {
    expect(conservativeWindowBackendScope('wikireader')).toEqual({ scope: 'profile', profile: 'wikireader' })
  })

  it('no profile (primary window) → primary scope', () => {
    expect(conservativeWindowBackendScope(null)).toEqual({ scope: 'primary', profile: null })
    expect(conservativeWindowBackendScope('')).toEqual({ scope: 'primary', profile: null })
  })
})

describe('setBackendStatus scope attribution', () => {
  it('a session backend failure is keyed ONLY to that session scope', () => {
    const key = setBackendStatus({ scope: 'session', profile: 'wikireader', sessionId: 'session-1' }, 'failed', {
      lastError: 'connection closed'
    })

    expect(key).toBe('session:wikireader:session-1')
    const status = getBackendStatus(key)
    expect(status?.scope).toBe('session')
    expect(status?.sessionId).toBe('session-1')
    expect(status?.state).toBe('failed')
    expect(status?.lastError).toBe('connection closed')
    // It did NOT bleed into the primary/global scope.
    expect(getBackendStatus('primary')).toBeUndefined()
  })

  it('a session failure is DISTINGUISHABLE from a primary/profile failure (Task 9 requirement 4)', () => {
    setBackendStatus({ scope: 'primary', profile: null }, 'failed', { lastError: 'install missing' })
    setBackendStatus({ scope: 'session', profile: 'wikireader', sessionId: 'session-1' }, 'failed', {
      lastError: 'session backend wedged'
    })

    const primary = getBackendStatus('primary')
    const session = getBackendStatus('session:wikireader:session-1')

    expect(primary?.scope).toBe('primary')
    expect(session?.scope).toBe('session')
    // A per-session failure carries its session identity and is never mistaken
    // for the global/primary one — the discriminator later UI + Task 10 need.
    expect(session?.sessionId).toBe('session-1')
    expect(primary?.sessionId).toBeUndefined()
    expect(primary?.lastError).not.toBe(session?.lastError)
  })

  it('upserts by key (later state replaces earlier) and clears', () => {
    setBackendStatus({ scope: 'session', profile: 'p', sessionId: 's' }, 'starting')
    expect(getBackendStatus('session:p:s')?.state).toBe('starting')

    setBackendStatus({ scope: 'session', profile: 'p', sessionId: 's' }, 'ready')
    expect(getBackendStatus('session:p:s')?.state).toBe('ready')
    expect(Object.keys($backendStatuses.get())).toHaveLength(1)

    clearBackendStatus('session:p:s')
    expect(getBackendStatus('session:p:s')).toBeUndefined()
  })
})
