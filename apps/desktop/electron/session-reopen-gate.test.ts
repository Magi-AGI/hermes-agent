import assert from 'node:assert/strict'

import { test } from 'vitest'

import { waitForSessionWindowFunctionallyReady } from './session-reopen-gate'

// A minimal fake ipcMain: tracks listeners per channel and lets a test fire
// one, mirroring the slice of Electron's ipcMain the gate actually touches.
function makeFakeIpcMain() {
  const listeners = new Map<string, Set<Function>>()

  return {
    on(channel: string, listener: Function) {
      if (!listeners.has(channel)) {
        listeners.set(channel, new Set())
      }

      listeners.get(channel)!.add(listener)
    },
    removeListener(channel: string, listener: Function) {
      listeners.get(channel)?.delete(listener)
    },
    emit(channel: string, event: unknown, payload: unknown) {
      for (const listener of listeners.get(channel) ?? []) {
        listener(event, payload)
      }
    },
    listenerCount(channel: string) {
      return listeners.get(channel)?.size ?? 0
    }
  }
}

function makeFakeWindow(webContentsId = 1) {
  const listeners: Record<string, Function> = {}
  let destroyed = false

  return {
    isDestroyed: () => destroyed,
    destroy() {
      destroyed = true
    },
    webContents: {
      id: webContentsId,
      once(event: string, handler: Function) {
        listeners[`webContents:${event}`] = handler
      }
    },
    once(event: string, handler: Function) {
      listeners[event] = handler
    },
    emit(event: string, ...args: unknown[]) {
      listeners[event]?.(...args)
    },
    emitWebContents(event: string, ...args: unknown[]) {
      listeners[`webContents:${event}`]?.(...args)
    }
  }
}

// Never resolves/rejects on its own — tests that need to observe a specific
// path (timeout, ready signal, did-fail-load, closed) without the health
// probe interfering pass this.
function neverSettles() {
  return new Promise<void>(() => {})
}

// A promise the test controls from the outside — for scenarios that need to
// resolve/reject the health probe at a specific moment (e.g. after the
// renderer signal already arrived), rather than immediately or never.
function makeControllable() {
  let resolve!: () => void
  let reject!: (err: Error) => void

  const promise = new Promise<void>((res, rej) => {
    resolve = res
    reject = rej
  })

  return { promise, resolve, reject }
}

test('created === false resolves immediately without waiting on anything', async () => {
  const ipcMain = makeFakeIpcMain()
  const win = makeFakeWindow()

  const result = await waitForSessionWindowFunctionallyReady(
    win as any,
    { created: false, profile: 'default' },
    { ipcMain, probeBackendHealth: neverSettles }
  )

  assert.deepEqual(result, { ok: true })
  // Never registered a listener at all -- a focused, pre-existing window
  // never gets isLoading()-style or any other readiness check applied to it.
  assert.equal(ipcMain.listenerCount('hermes:window:functional-ready'), 0)
})

test('created === true never fast-paths on window state -- it waits for an explicit signal', async () => {
  const ipcMain = makeFakeIpcMain()
  const win = makeFakeWindow()

  const pending = waitForSessionWindowFunctionallyReady(
    win as any,
    { created: true, profile: 'default' },
    { ipcMain, probeBackendHealth: () => Promise.resolve(), timeoutMs: 200 }
  )

  // Give the promise a tick to register its listener before asserting it
  // hasn't resolved -- a newly-created window must not resolve on tick 0.
  await new Promise(resolve => setTimeout(resolve, 5))
  assert.equal(ipcMain.listenerCount('hermes:window:functional-ready'), 1)

  ipcMain.emit('hermes:window:functional-ready', { sender: { id: 1 } }, { ok: true })

  const result = await pending

  assert.deepEqual(result, { ok: true })
})

test('functional-ready ok:false stops the queue for this window', async () => {
  const ipcMain = makeFakeIpcMain()
  const win = makeFakeWindow()

  const pending = waitForSessionWindowFunctionallyReady(
    win as any,
    { created: true, profile: 'claudetriad' },
    { ipcMain, probeBackendHealth: neverSettles, timeoutMs: 200 }
  )

  await new Promise(resolve => setTimeout(resolve, 5))
  ipcMain.emit('hermes:window:functional-ready', { sender: { id: 1 } }, {
    ok: false,
    phase: 'sessions',
    error: 'refreshSessions failed'
  })

  const result = await pending

  assert.equal(result.ok, false)
  assert.match(result.reason ?? '', /refreshSessions failed/)
})

test('ok:true for the WRONG profile does not release the queue', async () => {
  const ipcMain = makeFakeIpcMain()
  const win = makeFakeWindow()

  const pending = waitForSessionWindowFunctionallyReady(
    win as any,
    { created: true, profile: 'claudetriad' },
    { ipcMain, probeBackendHealth: neverSettles, timeoutMs: 200 }
  )

  await new Promise(resolve => setTimeout(resolve, 5))
  // Same webContents id, but reports having connected to a DIFFERENT
  // profile's backend -- a real routing bug, not a pass, even within the
  // documented profile-only v1 scope.
  ipcMain.emit('hermes:window:functional-ready', { sender: { id: 1 } }, { ok: true, profile: 'default' })

  const result = await pending

  assert.equal(result.ok, false)
  assert.match(result.reason ?? '', /expected "claudetriad"/)
})

test('ok:true with no profile field on the payload is treated as "default", and matches an expected default profile', async () => {
  const ipcMain = makeFakeIpcMain()
  const win = makeFakeWindow()

  const pending = waitForSessionWindowFunctionallyReady(
    win as any,
    { created: true, profile: null },
    { ipcMain, probeBackendHealth: () => Promise.resolve(), timeoutMs: 200 }
  )

  await new Promise(resolve => setTimeout(resolve, 5))
  ipcMain.emit('hermes:window:functional-ready', { sender: { id: 1 } }, { ok: true })

  const result = await pending

  assert.deepEqual(result, { ok: true })
})

test('a signal from a different window (different webContents id) is ignored', async () => {
  const ipcMain = makeFakeIpcMain()
  const win = makeFakeWindow(1)

  const pending = waitForSessionWindowFunctionallyReady(
    win as any,
    { created: true, profile: 'default' },
    { ipcMain, probeBackendHealth: neverSettles, timeoutMs: 30 }
  )

  await new Promise(resolve => setTimeout(resolve, 5))
  // Some OTHER window's signal must not release this one's wait.
  ipcMain.emit('hermes:window:functional-ready', { sender: { id: 999 } }, { ok: true })

  const result = await pending

  assert.equal(result.ok, false)
  assert.match(result.reason ?? '', /timed out/)
})

test('health gating is delegated to the injected probe, not a raw fetch', async () => {
  const ipcMain = makeFakeIpcMain()
  const win = makeFakeWindow()
  let probedProfile: string | null = null
  let probeSignal: AbortSignal | null = null
  const health = makeControllable()

  const pending = waitForSessionWindowFunctionallyReady(
    win as any,
    { created: true, profile: 'claudetriad' },
    {
      ipcMain,
      probeBackendHealth: (profile, signal) => {
        probedProfile = profile
        probeSignal = signal

        return health.promise
      },
      timeoutMs: 200
    }
  )

  await new Promise(resolve => setTimeout(resolve, 5))
  assert.equal(probedProfile, 'claudetriad')
  assert.ok(probeSignal instanceof AbortSignal)

  ipcMain.emit('hermes:window:functional-ready', { sender: { id: 1 } }, { ok: true, profile: 'claudetriad' })
  health.resolve()

  const result = await pending

  assert.deepEqual(result, { ok: true })
  // The probe's abort signal fires once the gate settles, so an in-flight
  // health probe doesn't outlive the wait it serves.
  assert.equal((probeSignal as unknown as AbortSignal).aborted, true)
})

test('renderer success does NOT short-circuit a still-pending health probe -- a later health rejection still fails closed', async () => {
  // Regression test for triad review round 4, Codex P1: a renderer ok:true
  // previously resolved the whole wait immediately, aborting the health
  // probe and discarding its verdict entirely -- so a backend that looked
  // fine to the renderer but the independent probe would have caught as
  // unhealthy could still release the queue. Both must confirm.
  const ipcMain = makeFakeIpcMain()
  const win = makeFakeWindow()
  const health = makeControllable()

  const pending = waitForSessionWindowFunctionallyReady(
    win as any,
    { created: true, profile: 'claudetriad' },
    { ipcMain, probeBackendHealth: () => health.promise, timeoutMs: 5_000 }
  )

  await new Promise(resolve => setTimeout(resolve, 5))
  // Renderer reports success FIRST -- must not resolve the overall wait yet.
  ipcMain.emit('hermes:window:functional-ready', { sender: { id: 1 } }, { ok: true, profile: 'claudetriad' })

  await new Promise(resolve => setTimeout(resolve, 5))
  health.reject(new Error('independent probe caught it: backend never became healthy'))

  const result = await pending

  assert.equal(result.ok, false)
  assert.match(result.reason ?? '', /independent probe caught it/)
})

test('renderer success followed by health TIMEOUT still fails closed, not a silent pass', async () => {
  const ipcMain = makeFakeIpcMain()
  const win = makeFakeWindow()

  const pending = waitForSessionWindowFunctionallyReady(
    win as any,
    { created: true, profile: 'default' },
    { ipcMain, probeBackendHealth: neverSettles, timeoutMs: 20 }
  )

  await new Promise(resolve => setTimeout(resolve, 5))
  ipcMain.emit('hermes:window:functional-ready', { sender: { id: 1 } }, { ok: true })

  const result = await pending

  assert.equal(result.ok, false)
  assert.match(result.reason ?? '', /timed out/)
})

test('both signals succeeding resolves ok:true regardless of arrival order (renderer first)', async () => {
  const ipcMain = makeFakeIpcMain()
  const win = makeFakeWindow()
  const health = makeControllable()

  const pending = waitForSessionWindowFunctionallyReady(
    win as any,
    { created: true, profile: 'default' },
    { ipcMain, probeBackendHealth: () => health.promise, timeoutMs: 5_000 }
  )

  await new Promise(resolve => setTimeout(resolve, 5))
  ipcMain.emit('hermes:window:functional-ready', { sender: { id: 1 } }, { ok: true })
  await new Promise(resolve => setTimeout(resolve, 5))
  health.resolve()

  const result = await pending

  assert.deepEqual(result, { ok: true })
})

test('both signals succeeding resolves ok:true regardless of arrival order (health first)', async () => {
  const ipcMain = makeFakeIpcMain()
  const win = makeFakeWindow()
  const health = makeControllable()

  const pending = waitForSessionWindowFunctionallyReady(
    win as any,
    { created: true, profile: 'default' },
    { ipcMain, probeBackendHealth: () => health.promise, timeoutMs: 5_000 }
  )

  await new Promise(resolve => setTimeout(resolve, 5))
  health.resolve()
  await new Promise(resolve => setTimeout(resolve, 5))
  ipcMain.emit('hermes:window:functional-ready', { sender: { id: 1 } }, { ok: true })

  const result = await pending

  assert.deepEqual(result, { ok: true })
})

test('a rejecting health probe fails the window closed, even with no renderer signal at all', async () => {
  const ipcMain = makeFakeIpcMain()
  const win = makeFakeWindow()

  const result = await waitForSessionWindowFunctionallyReady(
    win as any,
    { created: true, profile: 'claudetriad' },
    {
      ipcMain,
      probeBackendHealth: () => Promise.reject(new Error('backend never became healthy')),
      timeoutMs: 200
    }
  )

  assert.equal(result.ok, false)
  assert.match(result.reason ?? '', /backend never became healthy/)
})

test('timeout path fails closed with a reported reason, not a silent resolve', async () => {
  const ipcMain = makeFakeIpcMain()
  const win = makeFakeWindow()

  const result = await waitForSessionWindowFunctionallyReady(
    win as any,
    { created: true, profile: 'default' },
    { ipcMain, probeBackendHealth: neverSettles, timeoutMs: 20 }
  )

  assert.equal(result.ok, false)
  assert.match(result.reason ?? '', /timed out after 20ms/)
  // The listener must be cleaned up on timeout too, or every timed-out window
  // leaks an ipcMain listener for the life of the process.
  assert.equal(ipcMain.listenerCount('hermes:window:functional-ready'), 0)
})

test('a main-frame did-fail-load fails closed without waiting out the full timeout', async () => {
  const ipcMain = makeFakeIpcMain()
  const win = makeFakeWindow()

  const pending = waitForSessionWindowFunctionallyReady(
    win as any,
    { created: true, profile: 'default' },
    { ipcMain, probeBackendHealth: neverSettles, timeoutMs: 5_000 }
  )

  await new Promise(resolve => setTimeout(resolve, 5))
  // Electron's did-fail-load passes the Event object first, THEN the
  // (errorCode, errorDescription, validatedURL, isMainFrame) tuple.
  win.emitWebContents('did-fail-load', {}, -2, 'net::ERR_FAILED', 'hermes://session', true)

  const result = await pending

  assert.equal(result.ok, false)
  assert.match(result.reason ?? '', /failed to load/)
})

test('a non-main-frame did-fail-load is ignored (an iframe/subresource failing is not the window failing)', async () => {
  const ipcMain = makeFakeIpcMain()
  const win = makeFakeWindow()

  const pending = waitForSessionWindowFunctionallyReady(
    win as any,
    { created: true, profile: 'default' },
    { ipcMain, probeBackendHealth: neverSettles, timeoutMs: 20 }
  )

  await new Promise(resolve => setTimeout(resolve, 5))
  win.emitWebContents('did-fail-load', {}, -2, 'net::ERR_FAILED', 'hermes://sub', false)

  const result = await pending

  // Falls through to the timeout, not the did-fail-load path.
  assert.equal(result.ok, false)
  assert.match(result.reason ?? '', /timed out/)
})

test('the window closing mid-wait resolves ok -- a closed window is not a backend-health failure', async () => {
  const ipcMain = makeFakeIpcMain()
  const win = makeFakeWindow()

  const pending = waitForSessionWindowFunctionallyReady(
    win as any,
    { created: true, profile: 'default' },
    { ipcMain, probeBackendHealth: neverSettles, timeoutMs: 5_000 }
  )

  await new Promise(resolve => setTimeout(resolve, 5))
  win.emit('closed')

  const result = await pending

  assert.deepEqual(result, { ok: true })
})

test('an already-destroyed window resolves immediately without registering any listener', async () => {
  const ipcMain = makeFakeIpcMain()
  const win = makeFakeWindow()

  win.destroy()

  const result = await waitForSessionWindowFunctionallyReady(
    win as any,
    { created: true, profile: 'default' },
    { ipcMain, probeBackendHealth: neverSettles }
  )

  assert.deepEqual(result, { ok: true })
  assert.equal(ipcMain.listenerCount('hermes:window:functional-ready'), 0)
})

test('ok:true on a DIFFERENT session than the one the window was asked to open fails (Codex A4 #3)', async () => {
  const ipcMain = makeFakeIpcMain()
  const win = makeFakeWindow()

  const pending = waitForSessionWindowFunctionallyReady(
    win as any,
    { created: true, profile: 'default', sessionId: 's-target' },
    { ipcMain, probeBackendHealth: () => Promise.resolve(), timeoutMs: 500 }
  )

  await new Promise(resolve => setTimeout(resolve, 5))
  ipcMain.emit('hermes:window:functional-ready', { sender: { id: 1 } }, { ok: true, profile: 'default', sessionId: 's-other' })

  const result = await pending

  assert.equal(result.ok, false)
  assert.match(result.reason ?? '', /expected "s-target"/)
})

test('ok:true on the expected session releases the queue', async () => {
  const ipcMain = makeFakeIpcMain()
  const win = makeFakeWindow()

  const pending = waitForSessionWindowFunctionallyReady(
    win as any,
    { created: true, profile: 'default', sessionId: 's-target' },
    { ipcMain, probeBackendHealth: () => Promise.resolve(), timeoutMs: 500 }
  )

  await new Promise(resolve => setTimeout(resolve, 5))
  ipcMain.emit('hermes:window:functional-ready', { sender: { id: 1 } }, { ok: true, profile: 'default', sessionId: 's-target' })

  assert.equal((await pending).ok, true)
})
