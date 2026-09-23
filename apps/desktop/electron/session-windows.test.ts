import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  buildInstanceWindowUrl,
  buildSessionWindowUrl,
  chatWindowWebPreferences,
  createSessionWindowRegistry,
  instanceWindowBounds,
  loadSavedWindowsFile,
  readSavedWindowEntries,
  sanitizeSessionWindowEntry,
  sessionWindowIdForOpen,
  toSavedWindowEntry
} from './session-windows'

// A minimal fake BrowserWindow: tracks listeners + destroyed state and lets a
// test fire the 'closed' event, mirroring the slice of the Electron API the
// registry actually touches.
function makeFakeWindow() {
  const listeners = {}
  const calls = { focus: 0, show: 0, restore: 0 }
  let destroyed = false
  let minimized = false
  let visible = true

  return {
    on(event, handler) {
      listeners[event] = handler

      return this
    },
    emit(event) {
      listeners[event]?.()
    },
    isDestroyed: () => destroyed,
    destroy() {
      destroyed = true
    },
    isMinimized: () => minimized,
    setMinimized(value) {
      minimized = value
    },
    isVisible: () => visible,
    setVisible(value) {
      visible = value
    },
    restore() {
      calls.restore += 1
      minimized = false
    },
    show() {
      calls.show += 1
      visible = true
    },
    focus() {
      calls.focus += 1
    },
    calls
  }
}

test('buildSessionWindowUrl puts the secondary flag before the hash route (dev server)', () => {
  const url = buildSessionWindowUrl('abc123', { devServer: 'http://localhost:5173' })

  assert.equal(url, 'http://localhost:5173/?win=secondary#/abc123')
})

test('buildSessionWindowUrl avoids a double slash when the dev server has a trailing slash', () => {
  const url = buildSessionWindowUrl('abc123', { devServer: 'http://localhost:5173/' })

  assert.equal(url, 'http://localhost:5173/?win=secondary#/abc123')
})

test('buildSessionWindowUrl carries the owning profile in the query before the hash (#82768)', () => {
  const url = buildSessionWindowUrl('abc123', { devServer: 'http://localhost:5173', profile: 'work', watch: true })

  assert.equal(url, 'http://localhost:5173/?win=secondary&watch=1&profile=work#/abc123')
})

test('buildSessionWindowUrl encodes the session id in the hash route', () => {
  const url = buildSessionWindowUrl('a b/c', { devServer: 'http://localhost:5173' })

  // The query flag must precede the '#' or HashRouter would swallow it as the
  // route; the id is URL-encoded so slashes/spaces survive routeSessionId().
  assert.equal(url, 'http://localhost:5173/?win=secondary#/a%20b%2Fc')
  assert.ok(url.indexOf('?win=secondary') < url.indexOf('#'))
})

test('buildSessionWindowUrl builds a packaged file URL with the flag before the hash', () => {
  const url = buildSessionWindowUrl('abc', { rendererIndexPath: '/opt/app/index.html' })

  assert.match(url, /^file:\/\/.*index\.html\?win=secondary#\/abc$/)
})

test('buildSessionWindowUrl adds the watch flag for spectator windows, before the hash', () => {
  const url = buildSessionWindowUrl('abc', { devServer: 'http://localhost:5173', watch: true })

  assert.equal(url, 'http://localhost:5173/?win=secondary&watch=1#/abc')
})

test('buildInstanceWindowUrl marks a full peer without selecting a specialized renderer', () => {
  const url = buildInstanceWindowUrl({ devServer: 'http://localhost:5173/' })

  assert.equal(url, 'http://localhost:5173/?peer=1')
  assert.ok(!url.includes('win='))
})

test('buildInstanceWindowUrl marks a packaged full peer', () => {
  const url = buildInstanceWindowUrl({ rendererIndexPath: '/opt/app/index.html' })

  assert.match(url, /^file:\/\/.*index\.html\?peer=1$/)
})

test('full peers carry their boot owner but only explicit profile windows pin future chats', () => {
  for (const source of [{ devServer: 'http://localhost:5173/' }, { rendererIndexPath: '/opt/app/index.html' }]) {
    for (const connectionId of [null, 'remote&work']) {
      for (const profileWindow of [false, true]) {
        const url = new URL(buildInstanceWindowUrl({ ...source, connectionId, profile: 'work', profileWindow }))
        assert.equal(url.searchParams.get('peer'), '1')
        assert.equal(url.searchParams.get('profile'), 'work')
        assert.equal(url.searchParams.get('connectionId'), connectionId ?? '')
        assert.equal(url.searchParams.get('profileWindow'), profileWindow ? '1' : null)
        assert.equal(url.searchParams.has('win'), false)
        assert.equal(url.hash, '')
      }
    }
  }
})

test('instanceWindowBounds cascades a new window off its source bounds', () => {
  const bounds = instanceWindowBounds({ x: 100, y: 120, width: 1400, height: 900 }, { width: 1, height: 1 })

  assert.deepEqual(bounds, { width: 1400, height: 900, x: 132, y: 152 })
})

test('instanceWindowBounds keeps the cascaded window inside the work area it lands on', () => {
  const displays = [{ workArea: { x: 0, y: 0, width: 1920, height: 1040 } }]
  // Source docked at the bottom-right: a raw +32/+32 cascade would overshoot both edges.
  const bounds = instanceWindowBounds({ x: 700, y: 240, width: 1220, height: 800 }, { width: 1, height: 1 }, displays)

  assert.deepEqual(bounds, { width: 1220, height: 800, x: 700, y: 240 })
})

test('instanceWindowBounds falls back to the persisted geometry with no source window', () => {
  const fallback = { width: 1280, height: 800 }

  assert.equal(instanceWindowBounds(null, fallback), fallback)
})

test('registry opens one window per session and focuses on re-open', () => {
  const registry = createSessionWindowRegistry()
  let built = 0
  const win = makeFakeWindow()

  const factory = () => {
    built += 1

    return win
  }

  const first = registry.openOrFocus('s1', factory)
  const second = registry.openOrFocus('s1', factory)

  assert.equal(built, 1, 'factory runs once for the same session')
  assert.equal(first.win, second.win)
  assert.equal(first.created, true, 'the first open actually created the window')
  assert.equal(second.created, false, 'the second open only focused the existing window')
  assert.equal(registry.size, 1)
  assert.equal(win.calls.focus, 1, 'second open focuses the existing window')
})

test('registry restores + shows a minimized/hidden window on re-open', () => {
  const registry = createSessionWindowRegistry()
  const win = makeFakeWindow()
  registry.openOrFocus('s1', () => win)

  win.setMinimized(true)
  win.setVisible(false)
  registry.openOrFocus('s1', () => win)

  assert.equal(win.calls.restore, 1)
  assert.equal(win.calls.show, 1)
  assert.equal(win.calls.focus, 1)
})

test('registry drops the entry when the window closes', () => {
  const registry = createSessionWindowRegistry()
  const win = makeFakeWindow()
  registry.openOrFocus('s1', () => win)
  assert.equal(registry.size, 1)

  win.emit('closed')

  assert.equal(registry.size, 0)
  assert.equal(registry.has('s1'), false)
})

test('registry rebuilds a fresh window after the previous one was destroyed', () => {
  const registry = createSessionWindowRegistry()
  const first = makeFakeWindow()
  registry.openOrFocus('s1', () => first)
  first.destroy()

  let built = 0
  const second = makeFakeWindow()

  const result = registry.openOrFocus('s1', () => {
    built += 1

    return second
  })

  assert.equal(built, 1, 'a destroyed window is replaced, not focused')
  assert.equal(result.win, second)
  assert.equal(result.created, true, 'a rebuilt window counts as created, not focused')
})

test('registry ignores empty / non-string session ids', () => {
  const registry = createSessionWindowRegistry()
  let built = 0

  const factory = () => {
    built += 1

    return makeFakeWindow()
  }

  assert.deepEqual(registry.openOrFocus('', factory), { win: null, created: false })
  assert.deepEqual(registry.openOrFocus('   ', factory), { win: null, created: false })
  assert.deepEqual(registry.openOrFocus(null, factory), { win: null, created: false })
  assert.deepEqual(registry.openOrFocus(42, factory), { win: null, created: false })
  assert.equal(built, 0)
  assert.equal(registry.size, 0)
})

test('registry trims the session id before keying', () => {
  const registry = createSessionWindowRegistry()
  const win = makeFakeWindow()
  registry.openOrFocus('  s1  ', () => win)

  assert.equal(registry.has('s1'), true)
})

test('registry carries the owning profile through entries() for reopen/persistence (#82768/#61286)', () => {
  // Regression: "Reopen Saved Session Windows" reads entries() to persist
  // session-windows-state.json. A non-primary-profile session that loses its
  // profile here silently reopens against the primary backend on the next
  // launch, reproducing the exact bug upstream's dfa74b5815 fixed for direct
  // opens.
  const registry = createSessionWindowRegistry()
  const win = makeFakeWindow()

  registry.openOrFocus('s1', () => win, { profile: 'work' })

  const [entry] = registry.entries()

  assert.equal(entry.profile, 'work')
})

test('registry defaults profile to null when none is given', () => {
  const registry = createSessionWindowRegistry()
  const win = makeFakeWindow()

  registry.openOrFocus('s1', () => win)

  const [entry] = registry.entries()

  assert.equal(entry.profile, null)
})

test('sanitizeSessionWindowEntry preserves a saved non-primary profile on reopen', () => {
  const entry = sanitizeSessionWindowEntry({
    sessionId: 's1',
    x: 10,
    y: 20,
    width: 900,
    height: 700,
    profile: 'work'
  })

  assert.equal(entry.profile, 'work')
})

test('sanitizeSessionWindowEntry trims a saved profile and normalizes a blank one to null', () => {
  const trimmed = sanitizeSessionWindowEntry({
    sessionId: 's1',
    width: 900,
    height: 700,
    profile: '  work  '
  })

  assert.equal(trimmed.profile, 'work')

  const blank = sanitizeSessionWindowEntry({
    sessionId: 's1',
    width: 900,
    height: 700,
    profile: '   '
  })

  assert.equal(blank.profile, null)

  const missing = sanitizeSessionWindowEntry({
    sessionId: 's1',
    width: 900,
    height: 700
  })

  assert.equal(missing.profile, null)
})

test('chatWindowWebPreferences leaves background throttling to the runtime stream dial', () => {
  // Regression (both directions): a static `backgroundThrottling: false` here
  // pinned document.visibilityState to 'visible' forever, turning every
  // visibility-gated poll into an always-on timer (~20% CPU at idle,
  // minimized). Streaming's "paint while blurred" need is served by
  // stream-throttle.ts flipping setBackgroundThrottling at turn boundaries —
  // so the static flag must stay absent.
  const prefs = chatWindowWebPreferences('/tmp/preload.cjs')

  assert.equal('backgroundThrottling' in prefs, false)
})

test('chat renderer navigation stays passive while explicit window actions may focus', () => {
  const prefs = chatWindowWebPreferences('/tmp/preload.cjs')

  // In-page/SPA navigation can happen while a transcript keeps streaming. It
  // must not use Electron's default navigation focus path to activate Hermes.
  assert.equal(prefs.focusOnNavigation, false)

  // Re-opening a session is an explicit user action and must still raise the
  // existing window; the passive navigation guard does not disable that path.
  const registry = createSessionWindowRegistry()
  const win = makeFakeWindow()
  registry.openOrFocus('s1', () => win)
  registry.openOrFocus('s1', () => win)
  assert.equal(win.calls.focus, 1)
})

test('chatWindowWebPreferences passes the preload path through and keeps the hardened defaults', () => {
  const prefs = chatWindowWebPreferences('/some/preload.cjs')

  assert.equal(prefs.preload, '/some/preload.cjs')
  assert.equal(prefs.contextIsolation, true)
  assert.equal(prefs.sandbox, true)
  assert.equal(prefs.nodeIntegration, false)
})

test('chatWindowWebPreferences allows autoplay so wake-started voice speaks its first reply', () => {
  // Regression: Chromium's default autoplay policy suspends audio until a user
  // gesture. A wake-word-started voice conversation has no preceding click, so
  // the first reply's playback was rejected and only turn 2+ spoke. A native
  // app the user launched should not gate audio on a gesture.
  const prefs = chatWindowWebPreferences('/tmp/preload.cjs')

  assert.equal(prefs.autoplayPolicy, 'no-user-gesture-required')
})

// ─── Magi sync A4: per-window saved layout (criteria 2 and 3, migration) ──────

const BOUNDS = { x: 10, y: 20, width: 900, height: 700 }

test('two pop-outs on one session are two windows and two saved entries (criterion 2)', () => {
  const registry = createSessionWindowRegistry()
  const a = makeFakeWindow()
  const b = makeFakeWindow()

  const first = registry.openOrFocus(sessionWindowIdForOpen('s1'), () => a, { kind: 'session', sessionId: 's1' })
  const second = registry.openOrFocus(sessionWindowIdForOpen('s1'), () => b, { kind: 'session', sessionId: 's1' })

  assert.equal(first.created && second.created, true, 'a deliberate second window is a real second window')
  assert.notEqual(first.win, second.win)

  const saved = registry.entries().map(e => toSavedWindowEntry(e, BOUNDS, false))

  assert.equal(saved.length, 2)
  assert.deepEqual(saved.map(e => e.sessionId), ['s1', 's1'])
  assert.notEqual(saved[0].windowId, saved[1].windowId)
})

test('re-opening the SAME saved window focuses it instead of doubling it', () => {
  const registry = createSessionWindowRegistry()
  const win = makeFakeWindow()
  const id = sessionWindowIdForOpen('s1', { windowId: 'saved-1' })

  registry.openOrFocus(id, () => win, { kind: 'session', sessionId: 's1' })
  const again = registry.openOrFocus(sessionWindowIdForOpen('s1', { windowId: 'saved-1' }), () => makeFakeWindow())

  assert.equal(again.created, false)
  assert.equal(again.win, win)
})

test('watch windows keep focus-existing per session', () => {
  assert.equal(sessionWindowIdForOpen('s1', { watch: true }), sessionWindowIdForOpen('s1', { watch: true }))
})

test('a peer window is saved with its route and the session it is showing (criteria 2-3)', () => {
  const registry = createSessionWindowRegistry()
  const peer = makeFakeWindow()

  registry.openOrFocus('peer-1', () => peer, { kind: 'peer', profile: 'work', connectionId: 'c1', profileWindow: true })
  assert.equal(registry.setRoute(peer, { established: true, sessionId: 's9', profile: 'work', connectionId: 'c1' }), true)
  assert.equal(registry.setRoute(peer, { established: true, sessionId: 's9', profile: 'work', connectionId: 'c1' }), false, 'no-op when unchanged')

  const [saved] = registry.entries().map(e => toSavedWindowEntry(e, BOUNDS, true))

  assert.deepEqual(
    { windowId: saved.windowId, kind: saved.kind, sessionId: saved.sessionId, profile: saved.profile },
    { windowId: 'peer-1', kind: 'peer', sessionId: 's9', profile: 'work' }
  )
  assert.equal(saved.connectionId, 'c1')
  assert.equal(saved.profileWindow, true)

  // ...and round-trips through the reader unchanged.
  const [read] = readSavedWindowEntries([saved])

  assert.equal(read.kind, 'peer')
  assert.equal(read.sessionId, 's9')
  assert.equal(read.connectionId, 'c1')
})

test('a restored peer opens on its OWN saved session (criterion 3)', () => {
  const url = buildInstanceWindowUrl({ devServer: 'http://localhost:5174', profile: 'work', connectionId: 'c1', sessionId: 's9' })

  assert.match(url, /\?peer=1[^#]*#\/s9$/)
  assert.doesNotMatch(buildInstanceWindowUrl({ devServer: 'http://localhost:5174' }), /#/, 'a fresh peer has no session route')
})

test('a peer showing a fresh chat is still a saved entry', () => {
  const [read] = readSavedWindowEntries([{ windowId: 'p', kind: 'peer', sessionId: null, ...BOUNDS }])

  assert.equal(read.kind, 'peer')
  assert.equal(read.sessionId, null)
})

test('a legacy session-keyed state file migrates forward without losing entries', () => {
  const legacy = [
    { sessionId: 's1', ...BOUNDS, profile: 'work' },
    { sessionId: 's2', ...BOUNDS },
    { sessionId: 's1', ...BOUNDS } // two rows for one session: both kept
  ]

  const read = readSavedWindowEntries(legacy)

  assert.equal(read.length, 3)
  assert.deepEqual(read.map(e => e.kind), ['session', 'session', 'session'])
  assert.deepEqual(read.map(e => e.sessionId), ['s1', 's2', 's1'])
  assert.equal(new Set(read.map(e => e.windowId)).size, 3, 'every migrated entry has its own window id')
  assert.deepEqual(readSavedWindowEntries(legacy).map(e => e.windowId), read.map(e => e.windowId), 'ids are stable across reads')
})

// ─── Codex A4 #2: only ENOENT means an empty layout ──────────────────────────

test('a missing state file is an empty layout', () => {
  const missing = () => {
    throw Object.assign(new Error('nope'), { code: 'ENOENT' })
  }

  assert.deepEqual(loadSavedWindowsFile(missing), { ok: true, entries: [] })
})

test('any other read failure, unparsable JSON or a non-list is a FAILURE, never an empty layout', () => {
  const locked = () => {
    throw Object.assign(new Error('busy'), { code: 'EBUSY' })
  }

  assert.equal(loadSavedWindowsFile(locked).ok, false)
  assert.equal(loadSavedWindowsFile(() => '{"truncated": ').ok, false)
  assert.equal(loadSavedWindowsFile(() => '{"not": "a list"}').ok, false)
})

test('a readable file loads its entries', () => {
  const loaded = loadSavedWindowsFile(() => JSON.stringify([{ windowId: 'a', kind: 'session', sessionId: 's', ...BOUNDS }]))

  assert.equal(loaded.ok, true)
  assert.equal(loaded.ok && loaded.entries.length, 1)
})

// ─── Codex A4 #5: session + owner route saved and updated together ───────────

test('switching a window to a session on another backend updates session AND route together', () => {
  const registry = createSessionWindowRegistry()
  const peer = makeFakeWindow()

  registry.openOrFocus('peer-1', () => peer, { kind: 'peer', profile: 'work', connectionId: 'c1', sessionId: 's1' })
  assert.equal(registry.setRoute(peer, { established: true, sessionId: 's2', profile: 'remote-prof', connectionId: 'c2' }), true)

  const [saved] = registry.entries().map(e => toSavedWindowEntry(e, BOUNDS, false))

  assert.deepEqual(
    { sessionId: saved.sessionId, profile: saved.profile, connectionId: saved.connectionId },
    { sessionId: 's2', profile: 'remote-prof', connectionId: 'c2' }
  )
})

test('a route-only change (same session, owner now resolved elsewhere) is saved too', () => {
  const registry = createSessionWindowRegistry()
  const peer = makeFakeWindow()

  registry.openOrFocus('peer-1', () => peer, { kind: 'peer', profile: 'work', connectionId: null, sessionId: 's1' })
  assert.equal(registry.setRoute(peer, { established: true, sessionId: 's1', profile: 'work', connectionId: 'c9' }), true)
  assert.equal(registry.entries()[0].connectionId, 'c9')
})

// ─── Codex A4 #6: every restorable kind reports; drafts never lose an entry ───

test('a session pop-out moving to another session (e.g. via /new) is saved on the new one', () => {
  const registry = createSessionWindowRegistry()
  const pop = makeFakeWindow()

  registry.openOrFocus('pop-1', () => pop, { kind: 'session', sessionId: 's1', profile: 'work' })
  assert.equal(registry.setRoute(pop, { established: true, sessionId: 's2', profile: 'work', connectionId: null }), true)
  assert.equal(registry.entries()[0].sessionId, 's2')
})

test('a pop-out passing through a fresh draft keeps its saved session until the draft becomes one', () => {
  const registry = createSessionWindowRegistry()
  const pop = makeFakeWindow()

  registry.openOrFocus('pop-1', () => pop, { kind: 'session', sessionId: 's1', profile: 'work' })
  assert.equal(registry.setRoute(pop, { established: true, sessionId: null, profile: 'work', connectionId: null }), false)

  const [kept] = registry.entries().map(e => toSavedWindowEntry(e, BOUNDS, false))

  assert.equal(kept.sessionId, 's1')
  assert.equal(readSavedWindowEntries([kept]).length, 1, 'still a valid, reopenable saved entry')

  registry.setRoute(pop, { established: true, sessionId: 's3', profile: 'work', connectionId: null })
  assert.equal(registry.entries()[0].sessionId, 's3')
})

test('a peer on a fresh chat is recorded as such', () => {
  const registry = createSessionWindowRegistry()
  const peer = makeFakeWindow()

  registry.openOrFocus('peer-1', () => peer, { kind: 'peer', sessionId: 's1', profile: 'work' })
  assert.equal(registry.setRoute(peer, { established: true, sessionId: null, profile: 'work', connectionId: null }), true)
  assert.equal(registry.entries()[0].sessionId, null)
})

// ─── Codex A4 round 3 #4: initial renderer defaults are never a route ────────

test("a fresh peer's initial renderer defaults never overwrite its launch route", () => {
  const registry = createSessionWindowRegistry()
  const peer = makeFakeWindow()

  registry.openOrFocus('peer-1', () => peer, { kind: 'peer', profile: 'work', connectionId: 'c1', sessionId: null })

  // Before boot establishes the requested route: default profile, no connection.
  assert.equal(registry.setRoute(peer, { sessionId: null, profile: 'default', connectionId: null }), false)
  assert.equal(
    registry.setRoute(peer, { established: false, sessionId: null, profile: 'default', connectionId: null }),
    false
  )
  assert.deepEqual(
    { profile: registry.entries()[0].profile, connectionId: registry.entries()[0].connectionId },
    { profile: 'work', connectionId: 'c1' }
  )

  // Boot established the route: now it is authoritative.
  assert.equal(registry.setRoute(peer, { established: true, sessionId: 's1', profile: 'work', connectionId: 'c1' }), true)
  assert.equal(registry.entries()[0].sessionId, 's1')
})
