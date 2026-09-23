import assert from 'node:assert/strict'

import { test } from 'vitest'

import { createCloseIntentTracker } from './session-window-close-intent'

const running = { quitting: false }
const quitting = { quitting: true }

function userClose(tracker, win, id, opts = running) {
  tracker.onClose(win, id, opts)

  return tracker.onClosed(win, id, opts)
}

test('an individual user close (close then closed) removes the entry', () => {
  const t = createCloseIntentTracker()

  assert.equal(userClose(t, {}, 'a'), true)
})

test('two deliberate closes 300ms apart are two removals, not a timing verdict (Codex blocker 1)', async () => {
  const t = createCloseIntentTracker()

  assert.equal(userClose(t, {}, 'a'), true)
  await new Promise(resolve => setTimeout(resolve, 300))
  assert.equal(userClose(t, {}, 'b'), true)
})

test('a forced destroy or crash (closed with no close event) preserves the entry', () => {
  const t = createCloseIntentTracker()

  assert.equal(t.onClosed({}, 'a', running), false)
})

test('app quit preserves every entry, whenever it is detected', () => {
  const t = createCloseIntentTracker()
  const w = {}

  assert.equal(userClose(t, w, 'a', quitting), false)
  // intent recorded while running, but the app was quitting by the time it closed
  t.onClose(w, 'b', running)
  assert.equal(t.onClosed(w, 'b', quitting), false)
})

test('OS logout/shutdown preserves entries, including an intent already recorded', () => {
  const t = createCloseIntentTracker()
  const w = {}

  t.onClose(w, 'a', running)
  t.markShuttingDown()
  assert.equal(t.onClosed(w, 'a', running), false)
  assert.equal(userClose(t, {}, 'b'), false)
})

test('our own programmatic closes (Close All, reopen teardown) preserve the entry', () => {
  const t = createCloseIntentTracker()
  const w = {}

  t.markProgrammatic(w, 'a')
  assert.equal(userClose(t, w, 'a'), false)
})

test('a cancelled user close later completed programmatically preserves the entry', () => {
  const t = createCloseIntentTracker()
  const w = {}

  t.onClose(w, 'a', running) // user X, but a handler cancelled it
  t.markProgrammatic(w, 'a') // later: Close All
  assert.equal(t.onClosed(w, 'a', running), false)
})

test('an intent for one window never removes a different window with the same id', () => {
  const t = createCloseIntentTracker()
  const first = {}
  const second = {}

  t.onClose(first, 'a', running)
  assert.equal(t.onClosed(second, 'a', running), false)
})
