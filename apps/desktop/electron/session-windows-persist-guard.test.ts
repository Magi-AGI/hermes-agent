import assert from 'node:assert/strict'

import { test } from 'vitest'

import { mergeSavedWindows } from './session-windows-persist-guard'

const w = (windowId: string, sessionId = windowId, x = 0) => ({ windowId, sessionId, x })
const ids = (entries: { windowId: string }[]) => entries.map(e => e.windowId)

test('an explicit individual close removes exactly that saved entry (criterion 1)', () => {
  const saved = [w('a'), w('b'), w('c')]
  const { entries, refusedRemovals } = mergeSavedWindows(saved, [w('a'), w('c')], ['b'])

  assert.deepEqual(ids(entries), ['a', 'c'])
  assert.deepEqual(refusedRemovals, [])
})

test('closing the last window individually empties the list', () => {
  assert.deepEqual(mergeSavedWindows([w('a')], [], ['a']).entries, [])
})

test('windows that are simply not open (quit, crash, never reopened) are never removed', () => {
  const saved = [w('a'), w('b'), w('c')]

  assert.deepEqual(ids(mergeSavedWindows(saved, [], []).entries), ['a', 'b', 'c'])
  assert.deepEqual(ids(mergeSavedWindows(saved, [w('b')], []).entries), ['a', 'b', 'c'])
})

test('a cascade (more than one close in one batch) is refused (the real-world bug)', () => {
  const saved = Array.from({ length: 21 }, (_, i) => w(`s${i}`))
  const closed = saved.slice(1).map(e => e.windowId)
  const { entries, refusedRemovals } = mergeSavedWindows(saved, [saved[0]], closed)

  assert.equal(entries.length, 21, 'nothing is lost')
  assert.equal(refusedRemovals.length, 20)
})

test('after a cascade, a write that only ADDS windows goes through (new windows always recorded)', () => {
  // The saved list is larger than the live set (a partial crash cascade left it
  // that way); the user now opens two new windows. Under the old count guard
  // (saved 21 vs live 3) this write was refused forever.
  const saved = Array.from({ length: 21 }, (_, i) => w(`s${i}`))
  const live = [saved[0], w('new-1'), w('new-2')]
  const { entries, refusedRemovals } = mergeSavedWindows(saved, live, [])

  assert.equal(entries.length, 23)
  assert.deepEqual(ids(entries).slice(-2), ['new-1', 'new-2'])
  assert.deepEqual(refusedRemovals, [])
})

test('a cascade batch still records its additions while refusing the removals', () => {
  const saved = [w('a'), w('b'), w('c')]
  const { entries, refusedRemovals } = mergeSavedWindows(saved, [w('d')], ['a', 'b'])

  assert.deepEqual(ids(entries), ['a', 'b', 'c', 'd'])
  assert.deepEqual(refusedRemovals, ['a', 'b'])
})

test('live windows update their own entry in place, keyed per window (criterion 2)', () => {
  const saved = [w('x', 's1', 0), w('y', 's1', 0)]
  const { entries } = mergeSavedWindows(saved, [w('y', 's1', 50)], [])

  assert.deepEqual(entries, [w('x', 's1', 0), w('y', 's1', 50)])
})
