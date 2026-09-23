import { describe, expect, it } from 'vitest'

import { establishedWindowSessionReport, reportsWindowSession, windowSessionReport } from './window-session-report'

const active = { profile: 'work', connectionId: 'local' }

describe('windowSessionReport (Codex A4 #5)', () => {
  it('carries the session with its resolved EXACT owner route', () => {
    expect(windowSessionReport('s1', { profile: 'remote-prof', connectionId: 'c2' }, active)).toEqual({
      sessionId: 's1',
      profile: 'remote-prof',
      connectionId: 'c2'
    })
  })

  it('a bare-profile owner belongs to the window active connection', () => {
    expect(windowSessionReport('s1', 'other', active)).toEqual({ sessionId: 's1', profile: 'other', connectionId: 'local' })
  })

  it('an unknown owner or a fresh draft falls back to the active route', () => {
    expect(windowSessionReport('s1', undefined, active)).toEqual({ sessionId: 's1', ...active })
    expect(windowSessionReport(null, { profile: 'x', connectionId: 'y' }, active)).toEqual({ sessionId: null, ...active })
  })
})

describe('establishedWindowSessionReport (Codex A4 round 3 #4)', () => {
  const defaults = { sessionId: null, profile: 'default', connectionId: null }

  it('initial renderer defaults before boot establishes the route are not authoritative', () => {
    expect(establishedWindowSessionReport(defaults, 'renderer.starting').established).toBe(false)
    expect(establishedWindowSessionReport(defaults, 'renderer.config').established).toBe(false)
    expect(establishedWindowSessionReport(defaults, undefined).established).toBe(false)
    expect(establishedWindowSessionReport(defaults, 'renderer.error').established).toBe(false)
  })

  it('is authoritative once boot completed on the requested route', () => {
    expect(establishedWindowSessionReport({ sessionId: 's1', ...active }, 'renderer.ready')).toEqual({
      sessionId: 's1',
      ...active,
      established: true
    })
  })
})

describe('reportsWindowSession (Codex A4 #6)', () => {
  it('session pop-outs report too, not only peers', () => {
    expect(reportsWindowSession({ peer: false, secondary: true })).toBe(true)
    expect(reportsWindowSession({ peer: true, secondary: false })).toBe(true)
    expect(reportsWindowSession({ peer: false, secondary: false })).toBe(false)
  })
})
