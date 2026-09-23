import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  createSingleFlight,
  createWindowReadinessTracker,
  reopenedWindowPolicy,
  reopenProbeTarget,
  waitForSessionWindowFunctionallyReady
} from './session-reopen-gate'

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

test('a window with no recorded verdict never fast-paths on window state -- it waits for an explicit signal', async () => {
  const ipcMain = makeFakeIpcMain()
  const win = makeFakeWindow()

  const pending = waitForSessionWindowFunctionallyReady(
    win as any,
    { profile: 'default' },
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
    { profile: 'claudetriad' },
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
    { profile: 'claudetriad' },
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
    { profile: null },
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
    { profile: 'default' },
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
    { profile: 'claudetriad' },
    {
      ipcMain,
      probeBackendHealth: (target, signal) => {
        probedProfile = target.profile
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
    { profile: 'claudetriad' },
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
    { profile: 'default' },
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
    { profile: 'default' },
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
    { profile: 'default' },
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
    { profile: 'claudetriad' },
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
    { profile: 'default' },
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
    { profile: 'default' },
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
    { profile: 'default' },
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
    { profile: 'default' },
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
    { profile: 'default' },
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
    { profile: 'default', sessionId: 's-target' },
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
    { profile: 'default', sessionId: 's-target' },
    { ipcMain, probeBackendHealth: () => Promise.resolve(), timeoutMs: 500 }
  )

  await new Promise(resolve => setTimeout(resolve, 5))
  ipcMain.emit('hermes:window:functional-ready', { sender: { id: 1 } }, { ok: true, profile: 'default', sessionId: 's-target' })

  assert.equal((await pending).ok, true)
})

// ─── Codex A4 #4: real per-window readiness; one reopen at a time ────────────

test('an already-open window that reported ready is judged by its recorded verdict', async () => {
  const ipcMain = makeFakeIpcMain()
  const readiness = createWindowReadinessTracker(ipcMain)
  const win = makeFakeWindow()

  ipcMain.emit('hermes:window:functional-ready', { sender: { id: 1 } }, { ok: true, profile: 'default', sessionId: 's1' })

  const result = await waitForSessionWindowFunctionallyReady(
    win as any,
    { profile: 'default', sessionId: 's1' },
    { ipcMain, probeBackendHealth: () => Promise.resolve(), knownVerdict: readiness.get(1), timeoutMs: 500 }
  )

  assert.deepEqual(result, { ok: true })
})

test('an already-open window that FAILED is not treated as ready just because it was only focused', async () => {
  const ipcMain = makeFakeIpcMain()
  const readiness = createWindowReadinessTracker(ipcMain)
  const win = makeFakeWindow()

  ipcMain.emit('hermes:window:functional-ready', { sender: { id: 1 } }, { ok: false, error: 'boot failed' })

  const result = await waitForSessionWindowFunctionallyReady(
    win as any,
    { profile: 'default' },
    { ipcMain, probeBackendHealth: () => Promise.resolve(), knownVerdict: readiness.get(1), timeoutMs: 500 }
  )

  assert.equal(result.ok, false)
  assert.match(result.reason ?? '', /boot failed/)
})

test('an already-open window that has not reported yet (still booting) is waited for', async () => {
  const ipcMain = makeFakeIpcMain()
  const readiness = createWindowReadinessTracker(ipcMain)
  const win = makeFakeWindow()

  const result = await waitForSessionWindowFunctionallyReady(
    win as any,
    { profile: 'default' },
    { ipcMain, probeBackendHealth: () => Promise.resolve(), knownVerdict: readiness.get(1), timeoutMs: 50 }
  )

  assert.equal(result.ok, false)
  assert.match(result.reason ?? '', /timed out/)
})

test('a reload forgets the previous verdict and starts a new main-assigned load generation', () => {
  const ipcMain = makeFakeIpcMain()
  const readiness = createWindowReadinessTracker(ipcMain)

  ipcMain.emit('hermes:window:functional-ready', { sender: { id: 7 } }, { ok: true, generation: 99 })
  assert.deepEqual(readiness.get(7), { ok: true, generation: 0 }, 'main stamps the generation, not the renderer')
  readiness.reload(7)
  assert.equal(readiness.get(7), null)
  ipcMain.emit('hermes:window:functional-ready', { sender: { id: 7 } }, { ok: true })
  assert.deepEqual(readiness.get(7), { ok: true, generation: 1 })
})

test('concurrent Reopen requests share one queue', async () => {
  let runs = 0
  let release!: () => void

  const reopen = createSingleFlight(async () => {
    runs += 1
    await new Promise<void>(resolve => {
      release = resolve
    })
  })

  const first = reopen()
  const second = reopen()

  assert.equal(first, second, 'the second request joins the active queue')
  release()
  await first
  assert.equal(runs, 1)

  const third = reopen()

  release()
  await third
  assert.equal(runs, 2, 'a request after the queue finished starts a new one')
})

// ─── Codex A4 round 3 #1: current readiness; never destroy a user window ─────

test('an already-open window that moved to another session (/new) is judged on its CURRENT verdict', async () => {
  const ipcMain = makeFakeIpcMain()
  const readiness = createWindowReadinessTracker(ipcMain)
  const win = makeFakeWindow()

  // Launched on s-a, then /new moved it to s-b: it re-reported pending, then ready on s-b.
  ipcMain.emit('hermes:window:functional-ready', { sender: { id: 1 } }, { ok: true, profile: 'default', sessionId: 's-a' })
  ipcMain.emit(
    'hermes:window:functional-ready',
    { sender: { id: 1 } },
    { ok: false, pending: true, profile: 'default', sessionId: 's-b' }
  )
  ipcMain.emit('hermes:window:functional-ready', { sender: { id: 1 } }, { ok: true, profile: 'default', sessionId: 's-b' })

  const result = await waitForSessionWindowFunctionallyReady(
    win as any,
    { profile: 'default', sessionId: 's-b', ...reopenedWindowPolicy(false) },
    { ipcMain, probeBackendHealth: () => Promise.resolve(), knownVerdict: readiness.get(1), timeoutMs: 500 }
  )

  assert.deepEqual(result, { ok: true })
})

test('an already-open window is judged on being functional, not on a saved entry that lags it', async () => {
  const ipcMain = makeFakeIpcMain()
  const win = makeFakeWindow()

  // e.g. a pop-out on a fresh draft after /new: its saved entry still names s-a.
  const result = await waitForSessionWindowFunctionallyReady(
    win as any,
    { profile: 'default', sessionId: 's-a', identity: reopenedWindowPolicy(false).identity },
    {
      ipcMain,
      probeBackendHealth: () => Promise.resolve(),
      knownVerdict: { ok: true, profile: 'default', sessionId: null },
      timeoutMs: 500
    }
  )

  assert.deepEqual(result, { ok: true })
})

test('a window the queue created must still come up on the saved session', async () => {
  const ipcMain = makeFakeIpcMain()
  const win = makeFakeWindow()

  const result = await waitForSessionWindowFunctionallyReady(
    win as any,
    { profile: 'default', sessionId: 's-a', identity: reopenedWindowPolicy(true).identity },
    {
      ipcMain,
      probeBackendHealth: () => Promise.resolve(),
      knownVerdict: { ok: true, profile: 'default', sessionId: 's-other' },
      timeoutMs: 500
    }
  )

  assert.equal(result.ok, false)
})

test('a pending verdict is not a verdict: the gate waits for the real one', async () => {
  const ipcMain = makeFakeIpcMain()
  const readiness = createWindowReadinessTracker(ipcMain)
  const win = makeFakeWindow()

  ipcMain.emit(
    'hermes:window:functional-ready',
    { sender: { id: 1 } },
    { ok: false, pending: true, profile: 'default', sessionId: 's1' }
  )

  const pending = waitForSessionWindowFunctionallyReady(
    win as any,
    { profile: 'default', sessionId: 's1' },
    { ipcMain, probeBackendHealth: () => Promise.resolve(), knownVerdict: readiness.get(1), timeoutMs: 500 }
  )

  await new Promise(resolve => setTimeout(resolve, 5))
  ipcMain.emit('hermes:window:functional-ready', { sender: { id: 1 } }, { ok: true, profile: 'default', sessionId: 's1' })

  assert.deepEqual(await pending, { ok: true })
})

test('only a window the queue created is torn down on failure', () => {
  assert.equal(reopenedWindowPolicy(true).destroyOnFailure, true)
  assert.equal(reopenedWindowPolicy(false).destroyOnFailure, false)
})

// ─── Codex A4 round 4 #1: readiness and health belong to the LATEST identity ─

// A probe fake: each call is recorded and resolved/rejected by the test.
function makeProbes() {
  const calls: { target: unknown; signal: AbortSignal; ctl: ReturnType<typeof makeControllable> }[] = []

  return {
    calls,
    probe: (target: unknown, signal: AbortSignal) => {
      const ctl = makeControllable()

      calls.push({ target, signal, ctl })

      return ctl.promise
    }
  }
}

const tick = () => new Promise(resolve => setTimeout(resolve, 5))

const ready = (ipcMain: ReturnType<typeof makeFakeIpcMain>, payload: object) =>
  ipcMain.emit('hermes:window:functional-ready', { sender: { id: 1 } }, payload)

test('a later pending report clears an earlier ready: the queue does not advance on it', async () => {
  const ipcMain = makeFakeIpcMain()
  const win = makeFakeWindow()
  const probes = makeProbes()
  let settled: unknown = null

  void waitForSessionWindowFunctionallyReady(
    win as any,
    { profile: 'default', sessionId: 's1' },
    { ipcMain, probeBackendHealth: probes.probe as any, timeoutMs: 300 }
  ).then(r => (settled = r))

  ready(ipcMain, { ok: true, attempt: 1, profile: 'default', sessionId: 's1' })
  // A boot retry: the window's LATEST state is pending.
  ready(ipcMain, { ok: false, pending: true, attempt: 2, profile: 'default', sessionId: 's1' })

  for (const call of probes.calls) {
    call.ctl.resolve()
  }

  await tick()
  assert.equal(settled, null, 'must not advance while the latest state is pending')
})

test('baseline: one identity, ready and healthy, advances on a single probe', async () => {
  const ipcMain = makeFakeIpcMain()
  const win = makeFakeWindow()
  const probes = makeProbes()
  let settled: unknown = null

  void waitForSessionWindowFunctionallyReady(
    win as any,
    { profile: 'default', sessionId: 's1' },
    { ipcMain, probeBackendHealth: probes.probe as any, timeoutMs: 500 }
  ).then(r => (settled = r))

  ready(ipcMain, { ok: true, attempt: 1, profile: 'default', sessionId: 's1' })
  probes.calls[0].ctl.resolve()
  await tick()
  assert.deepEqual(settled, { ok: true }, 'baseline: attempt 1 ready + healthy advances')
})

test('after an identity change the queue advances only on the latest ready AND a probe run for it', async () => {
  const ipcMain = makeFakeIpcMain()
  const win = makeFakeWindow()
  const probes = makeProbes()
  let settled: unknown = null

  void waitForSessionWindowFunctionallyReady(
    win as any,
    { profile: 'default', sessionId: 's1' },
    { ipcMain, probeBackendHealth: probes.probe as any, timeoutMs: 500 }
  ).then(r => (settled = r))

  ready(ipcMain, { ok: true, attempt: 1, profile: 'default', sessionId: 's1' })
  ready(ipcMain, { ok: false, pending: true, attempt: 2, profile: 'default', sessionId: 's1' })

  // Pending (round 6): the stale probe is discarded and none is started for it.
  assert.equal(probes.calls.length, 1, 'pending authorizes no probe')
  assert.equal(probes.calls[0].signal.aborted, true, 'the stale probe is discarded')

  // The stale probe's result must not count.
  probes.calls[0].ctl.resolve()
  ready(ipcMain, { ok: true, attempt: 2, profile: 'default', sessionId: 's1' })
  assert.equal(probes.calls.length, 2, 'the latest routed report is probed')
  await tick()
  assert.equal(settled, null, 'latest ready, but its probe has not finished')

  probes.calls[1].ctl.resolve()
  await tick()
  assert.deepEqual(settled, { ok: true })
})

// ─── Codex A4 round 5: one report carries the COMPLETE identity ──────────────
// (generation, attempt, session, profile, connection); the gate probes exactly
// the reported route, and a probe satisfies only the identity it was run for.

test('A->B switch: the gate probes B from the readiness report itself, even while the route message lags', async () => {
  const ipcMain = makeFakeIpcMain()
  const win = makeFakeWindow()
  const probes = makeProbes()
  let settled: unknown = null

  // An existing window; its separate route report (routeOf) still says A.
  void waitForSessionWindowFunctionallyReady(
    win as any,
    { profile: 'prof-a', sessionId: 's-a', identity: 'functional', probeTarget: { profile: 'prof-a', connectionId: null } },
    {
      ipcMain,
      probeBackendHealth: probes.probe as any,
      knownVerdict: { ok: true, attempt: 1, profile: 'prof-a', connectionId: null, sessionId: 's-a' },
      timeoutMs: 500
    }
  ).then(r => (settled = r))

  ready(ipcMain, { ok: false, pending: true, attempt: 2, profile: 'prof-b', connectionId: 'conn-b', sessionId: 's-b' })
  ready(ipcMain, { ok: true, attempt: 2, profile: 'prof-b', connectionId: 'conn-b', sessionId: 's-b' })

  const last = probes.calls[probes.calls.length - 1]

  assert.deepEqual(last.target, { profile: 'prof-b', connectionId: 'conn-b' })

  for (const call of probes.calls.slice(0, -1)) {
    call.ctl.resolve()
  }

  await tick()
  assert.equal(settled, null, 'a probe of A never satisfies B')

  last.ctl.resolve()
  await tick()
  assert.deepEqual(settled, { ok: true })
})

test('a connection-only switch (same profile and session) re-probes the new connection', async () => {
  const ipcMain = makeFakeIpcMain()
  const win = makeFakeWindow()
  const probes = makeProbes()
  let settled: unknown = null

  void waitForSessionWindowFunctionallyReady(
    win as any,
    { profile: 'work', sessionId: 's1', identity: 'functional', probeTarget: { profile: 'work', connectionId: 'c1' } },
    { ipcMain, probeBackendHealth: probes.probe as any, timeoutMs: 500 }
  ).then(r => (settled = r))

  ready(ipcMain, { ok: true, attempt: 1, profile: 'work', connectionId: 'c1', sessionId: 's1' })
  ready(ipcMain, { ok: true, attempt: 1, profile: 'work', connectionId: 'c2', sessionId: 's1' })

  assert.deepEqual(probes.calls[probes.calls.length - 1].target, { profile: 'work', connectionId: 'c2' })
  probes.calls[0].ctl.resolve()
  await tick()
  assert.equal(settled, null, 'the c1 probe does not satisfy c2')

  probes.calls[probes.calls.length - 1].ctl.resolve()
  await tick()
  assert.deepEqual(settled, { ok: true })
})

test('a reloaded renderer reusing attempt 1 is not accepted on the pre-reload probe', async () => {
  const ipcMain = makeFakeIpcMain()
  const readiness = createWindowReadinessTracker(ipcMain)
  const win = makeFakeWindow()
  const probes = makeProbes()
  let settled: unknown = null
  const report = { attempt: 1, profile: 'default', connectionId: null, sessionId: 's1' }

  void waitForSessionWindowFunctionallyReady(
    win as any,
    { profile: 'default', sessionId: 's1', probeTarget: { profile: 'default', connectionId: null } },
    { ipcMain, readiness, probeBackendHealth: probes.probe as any, timeoutMs: 500 }
  ).then(r => (settled = r))

  ready(ipcMain, { ...report, ok: false, pending: true })
  probes.calls[0].ctl.resolve()
  await tick()

  // Reload (main bumps the load generation); the new renderer starts at attempt 1 again.
  readiness.reload(1)
  ready(ipcMain, { ...report, ok: true })
  await tick()
  assert.equal(settled, null, 'the pre-reload probe result does not count for the new load')
  assert.equal(probes.calls.length, 2, 'the new load is probed')

  probes.calls[1].ctl.resolve()
  await tick()
  assert.deepEqual(settled, { ok: true })
})

test('the launch probe is reused only when the first report matches its identity', async () => {
  const ipcMain = makeFakeIpcMain()
  const win = makeFakeWindow()
  const probes = makeProbes()
  let settled: unknown = null

  void waitForSessionWindowFunctionallyReady(
    win as any,
    { profile: 'prof-a', sessionId: 's1', probeTarget: { profile: 'prof-a', connectionId: null } },
    { ipcMain, probeBackendHealth: probes.probe as any, timeoutMs: 500 }
  ).then(r => (settled = r))

  // First report: same session and profile, but on another connection.
  ready(ipcMain, { ok: true, attempt: 0, profile: 'prof-a', connectionId: 'conn-x', sessionId: 's1' })

  assert.equal(probes.calls.length, 2, 'a mismatching first report is probed on its own route')
  assert.deepEqual(probes.calls[1].target, { profile: 'prof-a', connectionId: 'conn-x' })
  probes.calls[0].ctl.resolve()
  await tick()
  assert.equal(settled, null, 'the launch probe does not satisfy a different identity')

  probes.calls[1].ctl.resolve()
  await tick()
  assert.deepEqual(settled, { ok: true })
})

test('pending for B (owner route unresolved) blocks advance on A and authorizes no probe (Codex A4 r6 #2)', async () => {
  const ipcMain = makeFakeIpcMain()
  const win = makeFakeWindow()
  const probes = makeProbes()
  let settled: unknown = null

  void waitForSessionWindowFunctionallyReady(
    win as any,
    { profile: 'prof-a', sessionId: 's-a', identity: 'functional', probeTarget: { profile: 'prof-a', connectionId: null } },
    {
      ipcMain,
      probeBackendHealth: probes.probe as any,
      knownVerdict: { ok: true, attempt: 1, profile: 'prof-a', connectionId: null, sessionId: 's-a' },
      timeoutMs: 500
    }
  ).then(r => (settled = r))

  // /new -> B: pending is sent at once, with no route (B's owner not resolved).
  ready(ipcMain, { ok: false, pending: true, attempt: 2, profile: null, connectionId: null, sessionId: 's-b' })

  assert.equal(probes.calls.length, 1, 'pending authorizes no probe (no fallback-route probe)')
  probes.calls[0].ctl.resolve()
  await tick()
  assert.equal(settled, null, "A's probe does not advance a window that is now establishing B")

  ready(ipcMain, { ok: true, attempt: 2, profile: 'prof-b', connectionId: 'conn-b', sessionId: 's-b' })
  assert.deepEqual(probes.calls[1].target, { profile: 'prof-b', connectionId: 'conn-b' })
  probes.calls[1].ctl.resolve()
  await tick()
  assert.deepEqual(settled, { ok: true })
})

test('baseline: a first report matching the launch identity reuses the launch probe', async () => {
  const ipcMain = makeFakeIpcMain()
  const win = makeFakeWindow()
  const probes = makeProbes()
  let settled: unknown = null

  void waitForSessionWindowFunctionallyReady(
    win as any,
    { profile: 'prof-a', sessionId: 's1', probeTarget: { profile: 'prof-a', connectionId: null } },
    { ipcMain, probeBackendHealth: probes.probe as any, timeoutMs: 500 }
  ).then(r => (settled = r))

  ready(ipcMain, { ok: true, attempt: 0, profile: 'prof-a', connectionId: null, sessionId: 's1' })
  probes.calls[0].ctl.resolve()
  await tick()
  assert.equal(probes.calls.length, 1)
  assert.deepEqual(settled, { ok: true })
})

// ─── Codex A4 #7: the probe checks the exact route the window was opened on ──

test('a peer is probed on its recorded connection+profile, a pop-out on its profile', () => {
  const peer = { kind: 'peer', profile: 'work', connectionId: 'saved-c' }

  assert.deepEqual(reopenProbeTarget(peer, { profile: 'work', connectionId: 'remote-1' }, 'default'), {
    profile: 'work',
    connectionId: 'remote-1'
  })
  assert.deepEqual(reopenProbeTarget(peer, null, 'default'), { profile: 'work', connectionId: 'saved-c' })
  assert.deepEqual(reopenProbeTarget({ kind: 'session', profile: null, connectionId: 'x' }, null, 'claudetriad'), {
    profile: 'claudetriad',
    connectionId: null
  })
})

test('a healthy existing pop-out on profile B is not halted because saved profile A is offline', async () => {
  const ipcMain = makeFakeIpcMain()
  const win = makeFakeWindow()

  const probe = (target: { profile: null | string }) =>
    target.profile === 'prof-b' ? Promise.resolve() : Promise.reject(new Error('prof-a backend down'))

  const result = await waitForSessionWindowFunctionallyReady(
    win as any,
    {
      profile: 'prof-a',
      sessionId: 's-a',
      identity: reopenedWindowPolicy(false).identity,
      // Its saved opening route is A; the window REPORTS it is on B.
      probeTarget: reopenProbeTarget({ kind: 'session', profile: 'prof-a', connectionId: null }, null, 'default')
    },
    {
      ipcMain,
      probeBackendHealth: probe,
      knownVerdict: { ok: true, profile: 'prof-b', connectionId: null, sessionId: 's-b' },
      timeoutMs: 500
    }
  )

  assert.deepEqual(result, { ok: true })
})

test('a ready REMOTE peer is not failed because the local backend of the same profile is down', async () => {
  const ipcMain = makeFakeIpcMain()
  const win = makeFakeWindow()

  // Local "work" is down; the remote connection is healthy.
  const probe = (target: { connectionId: null | string }) =>
    target.connectionId === 'remote-1' ? Promise.resolve() : Promise.reject(new Error('local backend down'))

  const pending = waitForSessionWindowFunctionallyReady(
    win as any,
    { profile: 'work', probeTarget: { profile: 'work', connectionId: 'remote-1' } },
    { ipcMain, probeBackendHealth: probe, timeoutMs: 500 }
  )

  await new Promise(resolve => setTimeout(resolve, 5))
  // The report carries the window's resolved route (Codex A4 round 5).
  ipcMain.emit('hermes:window:functional-ready', { sender: { id: 1 } }, { ok: true, profile: 'work', connectionId: 'remote-1' })

  assert.deepEqual(await pending, { ok: true })
})
