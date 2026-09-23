import assert from 'node:assert/strict'

import { test } from 'vitest'

import { mergeSavedWindows, planSavedWindowsWrite } from './session-windows-persist-guard'

const w = (windowId: string, sessionId = windowId, x = 0) => ({ windowId, sessionId, x })
const ids = (entries: { windowId: string }[]) => entries.map(e => e.windowId)

test('an explicit individual close removes exactly that saved entry (criterion 1)', () => {
  const saved = [w('a'), w('b'), w('c')]
  const { entries, removed } = mergeSavedWindows(saved, [w('a'), w('c')], ['b'])

  assert.deepEqual(ids(entries), ['a', 'c'])
  assert.deepEqual(removed, ['b'])
})

test('closing the last window individually empties the list', () => {
  assert.deepEqual(mergeSavedWindows([w('a')], [], ['a']).entries, [])
})

test('windows that are simply not open (quit, crash, never reopened) are never removed', () => {
  const saved = [w('a'), w('b'), w('c')]

  assert.deepEqual(ids(mergeSavedWindows(saved, [], []).entries), ['a', 'b', 'c'])
  assert.deepEqual(ids(mergeSavedWindows(saved, [w('b')], []).entries), ['a', 'b', 'c'])
})

test('after a cascade, a write that only ADDS windows goes through (new windows always recorded)', () => {
  // The saved list is larger than the live set (a partial crash cascade left it
  // that way); the user now opens two new windows. Under the old count guard
  // (saved 21 vs live 3) this write was refused forever.
  const saved = Array.from({ length: 21 }, (_, i) => w(`s${i}`))
  const live = [saved[0], w('new-1'), w('new-2')]
  const { entries, removed } = mergeSavedWindows(saved, live, [])

  assert.equal(entries.length, 23)
  assert.deepEqual(ids(entries).slice(-2), ['new-1', 'new-2'])
  assert.deepEqual(removed, [])
})

test('live windows update their own entry in place, keyed per window (criterion 2)', () => {
  const saved = [w('x', 's1', 0), w('y', 's1', 0)]
  const { entries } = mergeSavedWindows(saved, [w('y', 's1', 50)], [])

  assert.deepEqual(entries, [w('x', 's1', 0), w('y', 's1', 50)])
})

test('several individually-intended closes in one write are all removed (no count/timing verdict)', () => {
  const saved = [w('a'), w('b'), w('c')]
  const { entries, removed } = mergeSavedWindows(saved, [w('d')], ['a', 'b'])

  assert.deepEqual(ids(entries), ['c', 'd'])
  assert.deepEqual(removed, ['a', 'b'])
})

test('a cascade (crash, quit, destroy) produces no close ids, so nothing is removed', () => {
  const saved = Array.from({ length: 21 }, (_, i) => w(`s${i}`))

  assert.equal(mergeSavedWindows(saved, [saved[0]], []).entries.length, 21)
})

// ─── Codex A4 #2: a failed read never erases the layout ──────────────────────

test('a failed load writes nothing and retains the pending close decisions', () => {
  const plan = planSavedWindowsWrite({ ok: false, error: 'unparsable' }, [w('new')], ['a'])

  assert.deepEqual(plan, { write: null, clearClosed: false, error: 'unparsable' })
})

test('a successful load merges, writes when changed, and clears applied closes', () => {
  const plan = planSavedWindowsWrite({ ok: true, entries: [w('a'), w('b')] }, [w('b')], ['a'])

  assert.equal(plan.clearClosed, true)
  assert.deepEqual(JSON.parse(plan.write), [w('b')])
  assert.equal(planSavedWindowsWrite({ ok: true, entries: [w('a')] }, [w('a')], []).write, null, 'unchanged: no write')
})
