import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  createSavedWindowLayout,
  mergeSavedWindows,
  planSavedWindowsWrite,
  type WindowCloseReason
} from './session-windows-persist-guard'

const w = (windowId: string, sessionId = windowId, x = 0) => ({ windowId, sessionId, x })
const ids = (entries: { windowId: string }[]) => entries.map(e => e.windowId)

// A saved layout over an in-memory file, driven the way main.ts drives it.
function harness(initial: ReturnType<typeof w>[] = []) {
  const file = { text: JSON.stringify(initial, null, 2) as null | string, unreadable: false }
  let open = [...initial]

  const layout = createSavedWindowLayout({
    load: () =>
      file.unreadable
        ? { ok: false as const, error: 'unparsable' }
        : { ok: true as const, entries: file.text ? JSON.parse(file.text) : [] },
    write: text => {
      file.text = text
    },
    live: () => open
  })

  return {
    layout,
    file,
    saved: () => ids(JSON.parse(file.text || '[]')),
    open: (...entries: ReturnType<typeof w>[]) => {
      open = [...open, ...entries]
      layout.persist()
    },
    close: (windowId: string, reason: WindowCloseReason) => {
      layout.onWindowClosed(windowId, reason)
      open = open.filter(e => e.windowId !== windowId)
      layout.persist()
    },
    forgetAndClose: (windowId: string) => {
      open = open.filter(e => e.windowId !== windowId)
      layout.forget(windowId)
    }
  }
}

const EVERY_CLOSE: WindowCloseReason[] = [
  'user-x',
  'user-ctrl-w',
  'user-alt-f4',
  'taskbar-close',
  'taskbar-close-all',
  'app-quit',
  'os-shutdown',
  'crash',
  'destroy',
  'close-all-session-windows',
  'reopen-teardown'
]

// ─── Lake: closes never remove ───────────────────────────────────────────────

for (const reason of EVERY_CLOSE) {
  test(`closing a window (${reason}) never removes its saved entry`, () => {
    const h = harness()

    h.open(w('a'), w('b'))
    h.close('a', reason)
    assert.deepEqual(h.saved(), ['a', 'b'])
  })
}

test('closing every window, one after another, keeps them all', () => {
  const h = harness()

  h.open(w('a'), w('b'), w('c'))

  for (const id of ['a', 'b', 'c']) {
    h.close(id, 'user-x')
  }

  assert.deepEqual(h.saved(), ['a', 'b', 'c'])
})

test('a normally closed window comes back on Reopen (its saved entry is read back)', () => {
  const h = harness()

  h.open(w('a', 's1', 40), w('b'))
  h.close('a', 'user-x')

  const reopened = JSON.parse(h.file.text || '[]')

  assert.deepEqual(reopened.find(e => e.windowId === 'a'), w('a', 's1', 40))
})

// ─── Close and Forget This Window: the only removal ─────────────────────────

test('Forget removes exactly that entry (and the window is closed)', () => {
  const h = harness()

  h.open(w('a'), w('b'), w('c'))
  h.forgetAndClose('b')
  assert.deepEqual(h.saved(), ['a', 'c'])
})

test('a forgotten window that is still closing is never re-recorded', () => {
  const h = harness([w('a'), w('b')])

  // forget() runs before the window finishes closing: it is still live here.
  h.layout.forget('b')
  assert.deepEqual(h.saved(), ['a'])
})

test('Forget on an unreadable state file writes nothing and keeps the forget pending', () => {
  const h = harness([w('a'), w('b')])

  h.file.unreadable = true
  const before = h.file.text

  assert.equal(h.layout.forget('b'), false)
  assert.equal(h.file.text, before, 'nothing written over the unreadable file')
  assert.equal(h.layout.isForgotten('b'), true, 'the forget is retained')

  h.file.unreadable = false
  assert.equal(h.layout.persist(), true)
  assert.deepEqual(h.saved(), ['a'], 'applied on the next successful write')
  assert.equal(h.layout.isForgotten('b'), false)
})

// ─── merge / plan ────────────────────────────────────────────────────────────

test('live windows update their own entry in place and new windows are appended', () => {
  const { entries } = mergeSavedWindows([w('x', 's1', 0), w('y', 's1', 0)], [w('y', 's1', 50), w('z')], [])

  assert.deepEqual(entries, [w('x', 's1', 0), w('y', 's1', 50), w('z')])
})

test('a failed load writes nothing and retains pending forgets', () => {
  assert.deepEqual(planSavedWindowsWrite({ ok: false, error: 'unparsable' }, [w('new')], ['a']), {
    write: null,
    clearForgets: false,
    error: 'unparsable'
  })
})
