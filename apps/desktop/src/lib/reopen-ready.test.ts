import { describe, expect, it } from 'vitest'

import { bootFunctionalState } from '@/app/contrib/hooks/use-reopen-ready-signal'

import { createReopenReadySignaller, reopenReadyVerdict } from './reopen-ready'

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

describe('createReopenReadySignaller (Codex A4 round 3 #1)', () => {
  it('re-reports for the CURRENT session after /new moves the window', () => {
    const sent: unknown[] = []
    const signal = createReopenReadySignaller(payload => sent.push(payload))

    signal({ ...base, target: 's-a', selected: 's-a', runtime: 'r1' })
    // /new: the window now routes to s-b, which is still loading.
    signal({ ...base, target: 's-b', selected: 's-a', runtime: 'r1' })
    signal({ ...base, target: 's-b', selected: 's-b', runtime: 'r2' })

    expect(sent).toEqual([
      { ok: true, phase: 'session', profile: 'work', sessionId: 's-a', attempt: 0 },
      { ok: false, pending: true, phase: 'pending', profile: 'work', sessionId: 's-b', attempt: 1 },
      { ok: true, phase: 'session', profile: 'work', sessionId: 's-b', attempt: 1 }
    ])
  })

  it('re-reports a boot retry (ready -> pending -> ready) and a route change', () => {
    const sent: { ok: boolean; phase: string; profile: null | string }[] = []
    const signal = createReopenReadySignaller(payload => sent.push(payload))

    signal({ ...base, target: null })
    signal({ ...base, target: null, boot: 'pending' })
    signal({ ...base, target: null })
    signal({ ...base, target: null, profile: 'other' })

    expect(sent.map(p => [p.phase, p.profile])).toEqual([
      ['boot', 'work'],
      ['pending', 'work'],
      ['boot', 'work'],
      ['boot', 'other']
    ])
  })

  it('stamps each new attempt: entering pending starts one, and its verdict carries it (Codex A4 r4 #1)', () => {
    const sent: { attempt?: number; phase: string }[] = []
    const signal = createReopenReadySignaller(payload => sent.push(payload))

    signal({ ...base, target: null, boot: 'pending' })
    signal({ ...base, target: null })
    signal({ ...base, target: null, boot: 'pending' })
    signal({ ...base, target: null })

    expect(sent.map(p => [p.phase, p.attempt])).toEqual([
      ['pending', 1],
      ['boot', 1],
      ['pending', 2],
      ['boot', 2]
    ])
  })

  it('does not re-send an unchanged verdict', () => {
    const sent: unknown[] = []
    const signal = createReopenReadySignaller(payload => sent.push(payload))

    signal({ ...base, target: null })
    signal({ ...base, target: null })
    expect(sent).toHaveLength(1)
  })
})

describe('bootFunctionalState', () => {
  it('maps the boot store phases', () => {
    expect(bootFunctionalState({ phase: 'renderer.ready' })).toBe('ready')
    expect(bootFunctionalState({ phase: 'renderer.error', error: 'x' })).toEqual({ error: 'x' })
    expect(bootFunctionalState({ phase: 'renderer.config' })).toBe('pending')
  })
})
