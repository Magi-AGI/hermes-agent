import { describe, expect, it } from 'vitest'

import { bootFunctionalState } from '@/app/contrib/hooks/use-reopen-ready-signal'

import { createReopenReadySignaller, reopenReadyVerdict } from './reopen-ready'

const base = {
  boot: 'ready' as const,
  target: 's1',
  selected: null,
  runtime: null,
  exhausted: null,
  profile: 'work',
  connectionId: null
}

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
      connectionId: null,
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
    expect(reopenReadyVerdict({ ...base, target: null })).toEqual({ ok: true, phase: 'boot', profile: 'work', connectionId: null, sessionId: null })
  })
})

describe('createReopenReadySignaller (Codex A4 rounds 3-6)', () => {
  const ambient = { profile: 'work', connectionId: null }
  const state = { boot: 'ready' as const, target: 's1', selected: null, runtime: null, exhausted: null, ambient }

  // Drives the signaller the way the hook does; `resolve` is the owner route.
  function harness() {
    const sent: any[] = []
    const signaller = createReopenReadySignaller(payload => sent.push(payload))

    return {
      sent,
      signaller,
      step: (s: typeof state | any, resolve?: { profile: null | string; connectionId: null | string }) => {
        const step = signaller.update(s)

        if (step && resolve) {
          signaller.settle(step, resolve)
        }

        return step
      }
    }
  }

  it('an establishment change sends pending IMMEDIATELY, before the owner route resolves (round 6 #2)', () => {
    const h = harness()

    h.step({ ...state, target: 's-a', selected: 's-a', runtime: 'r1' }, { profile: 'prof-a', connectionId: null })
    h.sent.length = 0

    // /new -> s-b, which is ALREADY loaded: the verdict is ready at once, but
    // its owner route has not resolved yet.
    const step = h.signaller.update({ ...state, target: 's-b', selected: 's-b', runtime: 'r2' })

    expect(h.sent).toEqual([
      { ok: false, pending: true, phase: 'pending', profile: null, connectionId: null, sessionId: 's-b', attempt: 2 }
    ])

    // Only once resolved is READY sent — with the resolved route.
    h.signaller.settle(step!, { profile: 'prof-b', connectionId: 'conn-b' })
    expect(h.sent[1]).toEqual({
      ok: true,
      phase: 'session',
      profile: 'prof-b',
      connectionId: 'conn-b',
      sessionId: 's-b',
      attempt: 2
    })
  })

  it('a pending report carries no route (never authorizes a probe on a fallback route)', () => {
    const h = harness()

    h.step({ ...state, boot: 'pending' })
    expect(h.sent).toEqual([
      { ok: false, pending: true, phase: 'pending', profile: null, connectionId: null, sessionId: 's1', attempt: 1 }
    ])
  })

  it('a superseded resolution is dropped', () => {
    const h = harness()

    const stale = h.signaller.update({ ...state, target: 's-a', selected: 's-a', runtime: 'r1' })

    h.signaller.update({ ...state, target: 's-b', selected: 's-b', runtime: 'r2' })
    h.signaller.settle(stale!, { profile: 'prof-a', connectionId: null })
    expect(h.sent.every(p => p.sessionId !== 's-a' || p.pending)).toBe(true)
  })

  it('each establishment change is a new attempt: boot retry, target change, ambient route change', () => {
    const h = harness()
    const route = { profile: 'work', connectionId: null }

    h.step({ ...state, target: null }, route)
    h.step({ ...state, target: null, boot: 'pending' })
    h.step({ ...state, target: null }, route)
    h.step({ ...state, target: null, ambient: { profile: 'work', connectionId: 'c2' } }, { profile: 'work', connectionId: 'c2' })

    expect(h.sent.map(p => [p.phase, p.attempt])).toEqual([
      ['pending', 1],
      ['boot', 1],
      ['pending', 2],
      ['pending', 3],
      ['boot', 3],
      ['pending', 4],
      ['boot', 4]
    ])
  })

  it('carries the fully resolved owner route, connection included (round 5)', () => {
    const h = harness()

    h.step({ ...state, selected: 's1', runtime: 'r1' }, { profile: 'prof-b', connectionId: 'conn-b' })
    expect(h.sent[h.sent.length - 1]).toMatchObject({ ok: true, profile: 'prof-b', connectionId: 'conn-b', sessionId: 's1' })
  })

  it('does not re-send an unchanged verdict, but re-resolves one whose resolution was cancelled', () => {
    const h = harness()

    const first = h.signaller.update({ ...state, target: null })
    const again = h.signaller.update({ ...state, target: null })

    expect(again).toBe(first)
    h.signaller.settle(again!, { profile: 'work', connectionId: null })
    expect(h.signaller.update({ ...state, target: null })).toBeNull()
    expect(h.sent.filter(p => !p.pending)).toHaveLength(1)
  })
})

describe('bootFunctionalState', () => {
  it('maps the boot store phases', () => {
    expect(bootFunctionalState({ phase: 'renderer.ready' })).toBe('ready')
    expect(bootFunctionalState({ phase: 'renderer.error', error: 'x' })).toEqual({ error: 'x' })
    expect(bootFunctionalState({ phase: 'renderer.config' })).toBe('pending')
  })
})
