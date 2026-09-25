import assert from 'node:assert/strict'
import { type ChildProcess, spawn } from 'node:child_process'
import { once } from 'node:events'

import { afterEach, test } from 'vitest'

import { assessLiveness, createBackendLivenessSupervisor, freshLivenessWindow } from './backend-liveness'

const budget = { maxGapMs: 50_000, unresponsiveMs: 120_000 }

test('silence for the whole budget while alive is unresponsive; a single answer resets it', () => {
  let window = freshLivenessWindow()
  let verdict = { unresponsive: false, window }

  for (const at of [0, 15_000, 30_000, 45_000, 60_000, 75_000, 90_000, 105_000]) {
    verdict = assessLiveness(window, false, at, budget)
    window = verdict.window
  }

  assert.equal(verdict.unresponsive, false, 'not yet 120 s of silence')
  window = assessLiveness(window, true, 110_000, budget).window
  verdict = assessLiveness(window, false, 125_000, budget)
  assert.equal(verdict.unresponsive, false, 'an answer restarts the window')

  window = freshLivenessWindow()

  for (let at = 0; at <= 120_000; at += 15_000) {
    verdict = assessLiveness(window, false, at, budget)
    window = verdict.window
  }

  assert.equal(verdict.unresponsive, true)
})

test('a gap between probes (machine asleep) restarts the window instead of counting as silence', () => {
  let window = assessLiveness(freshLivenessWindow(), false, 0, budget).window
  window = assessLiveness(window, false, 15_000, budget).window
  // The laptop slept for an hour between two probes.
  const verdict = assessLiveness(window, false, 3_615_000, budget)

  assert.equal(verdict.unresponsive, false)
  assert.equal(verdict.window.firstFailureAt, 3_615_000)
})

// A real child process whose event loop freezes on demand (the loop blocked, the process alive,
// the listening socket still bound: the 2026-09-07 signature, from outside).
const FREEZABLE_SERVER = `
const http = require('node:http')
const server = http.createServer((req, res) => {
  if (req.url === '/freeze') { res.end('ok'); setTimeout(() => { for (;;) {} }, 10); return }
  res.end('{}')
})
server.listen(0, '127.0.0.1', () => process.stdout.write(String(server.address().port) + '\\n'))
`

const children: ChildProcess[] = []

afterEach(() => {
  for (const child of children.splice(0)) {
    child.kill('SIGKILL')
  }
})

async function startServer(): Promise<{ baseUrl: string; child: ChildProcess }> {
  const child = spawn(process.execPath, ['-e', FREEZABLE_SERVER], { stdio: ['ignore', 'pipe', 'inherit'] })
  children.push(child)
  const [chunk] = (await once(child.stdout!, 'data')) as [Buffer]

  return { baseUrl: `http://127.0.0.1:${chunk.toString().trim()}`, child }
}

const alive = (child: ChildProcess) => child.exitCode === null && child.signalCode === null

test('a backend whose loop froze is dumped, then killed; a healthy one is left alone', async () => {
  const frozen = await startServer()
  const healthy = await startServer()
  const events: string[] = []

  const supervisor = createBackendLivenessSupervisor({
    intervalMs: 100,
    isAlive: pid => [frozen.child, healthy.child].some(c => c.pid === pid && alive(c)),
    log: () => undefined,
    probeTimeoutMs: 200,
    unresponsiveMs: 1_000
  })

  for (const [key, server] of [
    ['frozen', frozen],
    ['healthy', healthy]
  ] as const) {
    supervisor.watch(key, {
      baseUrl: server.baseUrl,
      captureDumps: async () => void events.push(`dump:${key}:${alive(server.child) ? 'alive' : 'dead'}`),
      kill: () => {
        events.push(`kill:${key}`)
        server.child.kill('SIGKILL')
      },
      pid: server.child.pid!
    })
  }

  await fetch(`${frozen.baseUrl}/freeze`)
  const exited = once(frozen.child, 'exit')
  await Promise.race([exited, new Promise((_, reject) => setTimeout(() => reject(new Error('never restarted')), 8_000))])

  assert.deepEqual(events, ['dump:frozen:alive', 'kill:frozen'], 'native dumps are taken BEFORE the kill')
  assert.equal(alive(healthy.child), true)
  assert.deepEqual(supervisor.watchedKeys(), ['healthy'])
  supervisor.unwatch('healthy')
})

// A backend that never answers, driven through a scripted probe so each capture can be steered.
function silentSupervisor(onCapture: (ctl: ReturnType<typeof createBackendLivenessSupervisor>) => Promise<void>) {
  const kills: number[] = []
  let answer = false

  const supervisor = createBackendLivenessSupervisor({
    intervalMs: 20,
    isAlive: () => true,
    log: () => undefined,
    probe: async () => answer,
    probeTimeoutMs: 10,
    unresponsiveMs: 100
  })

  supervisor.watch('primary', {
    baseUrl: 'http://127.0.0.1:1',
    captureDumps: () => onCapture(supervisor),
    kill: () => void kills.push(4242),
    pid: 4242
  })

  return { kills, setAnswer: (value: boolean) => (answer = value), supervisor }
}

const settle = (ms: number) => new Promise(resolve => setTimeout(resolve, ms))

test('a teardown that begins while dumps are being written cancels the kill', async () => {
  let captured!: () => void
  const capturing = new Promise<void>(resolve => (captured = resolve))

  const { kills, supervisor } = silentSupervisor(async ctl => {
    // Quit / profile switch / repair starts its intentional stop mid-capture.
    ctl.unwatch('primary')
    captured()
  })

  await capturing
  await settle(100)

  assert.deepEqual(kills, [])
  assert.deepEqual(supervisor.watchedKeys(), [])
})

test('a backend that answers again by the end of the dump is not killed and stays watched', async () => {
  let captured!: () => void
  const capturing = new Promise<void>(resolve => (captured = resolve))
  let recover = () => undefined as unknown

  const run = silentSupervisor(async () => {
    recover()
    captured()
  })

  recover = () => run.setAnswer(true)
  await capturing
  await settle(100)

  assert.deepEqual(run.kills, [])
  assert.deepEqual(run.supervisor.watchedKeys(), ['primary'])
  run.supervisor.unwatchAll()
})
