import { describe, expect, it } from 'vitest'

import { bootFunctionalState } from '@/app/contrib/hooks/use-reopen-ready-signal'

import { reopenReadyVerdict } from './reopen-ready'

const base = { boot: 'ready' as const, target: 's1', selected: null, runtime: null, exhausted: null, profile: 'work' }

describe('reopenReadyVerdict (Codex A4 #3)', () => {
  it('is pending until every required boot op succeeded', () => {
    expect(reopenReadyVerdict({ ...base, boot: 'pending', selected: 's1', runtime: 'r1' })).toBeNull()
  })

  it('is NOT ready when boot succeeded but the target session has not loaded (the old sessions-list signal)', () => {
    expect(reopenReadyVerdict(base)).toBeNull()
    expect(reopenReadyVerdict({ ...base, selected: 's1' })).toBeNull()
    expect(reopenReadyVerdict({ ...base, selected: 's2', runtime: 'r2' })).toBeNull()
  })

  it('is ready once the target session is selected and bound to a runtime, carrying its identity', () => {
    expect(reopenReadyVerdict({ ...base, selected: 's1', runtime: 'r1' })).toEqual({
      ok: true,
      phase: 'session',
      profile: 'work',
      sessionId: 's1'
    })
  })

  it('reports ok:false when boot failed or the target resume is exhausted', () => {
    expect(reopenReadyVerdict({ ...base, boot: { error: 'config fetch failed' } })).toMatchObject({
      ok: false,
      phase: 'boot',
      error: 'config fetch failed'
    })
    expect(reopenReadyVerdict({ ...base, exhausted: 's1' })).toMatchObject({ ok: false, phase: 'session', sessionId: 's1' })
  })

  it('a window with no target session (a restored peer on a fresh chat) is ready after boot', () => {
    expect(reopenReadyVerdict({ ...base, target: null })).toEqual({ ok: true, phase: 'boot', profile: 'work', sessionId: null })
  })
})

describe('bootFunctionalState', () => {
  it('maps the boot store phases', () => {
    expect(bootFunctionalState({ phase: 'renderer.ready' })).toBe('ready')
    expect(bootFunctionalState({ phase: 'renderer.error', error: 'x' })).toEqual({ error: 'x' })
    expect(bootFunctionalState({ phase: 'renderer.config' })).toBe('pending')
  })
})
