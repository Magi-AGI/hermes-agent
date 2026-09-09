import assert from 'node:assert/strict'

import { test } from 'vitest'

import { shouldSkipShrinkingSessionWindowsWrite } from './session-windows-persist-guard'

test('a deliberate single-window close (N-1) is allowed', () => {
  assert.equal(shouldSkipShrinkingSessionWindowsWrite(21, 20), false)
  assert.equal(shouldSkipShrinkingSessionWindowsWrite(1, 0), false)
})

test('growth and no-change are allowed', () => {
  assert.equal(shouldSkipShrinkingSessionWindowsWrite(5, 5), false)
  assert.equal(shouldSkipShrinkingSessionWindowsWrite(5, 8), false)
  assert.equal(shouldSkipShrinkingSessionWindowsWrite(0, 3), false)
})

test('a partial bulk-close cascade landing above zero is blocked (the real-world bug)', () => {
  // The three actual recurrences from hermes-session-window-list-wipe.md.
  assert.equal(shouldSkipShrinkingSessionWindowsWrite(21, 1), true)
  assert.equal(shouldSkipShrinkingSessionWindowsWrite(31, 1), true)
  assert.equal(shouldSkipShrinkingSessionWindowsWrite(13, 1), true)
})

test('a drop of exactly 2 or more is blocked regardless of scale', () => {
  assert.equal(shouldSkipShrinkingSessionWindowsWrite(3, 1), true)
  // 2 -> 0 (a drop of 2) is also blocked here — persistSessionWindowsState's
  // separate, earlier empty-guard (`entries.length === 0`) already returns
  // before this function is ever reached for that exact case, so the two
  // guards are consistent, not contradictory.
  assert.equal(shouldSkipShrinkingSessionWindowsWrite(2, 0), true)
})
