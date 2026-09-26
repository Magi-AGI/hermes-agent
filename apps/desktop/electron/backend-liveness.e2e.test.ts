// Opt-in end-to-end run of the D7 liveness supervisor against a REAL frozen backend. Skipped unless the
// concurrency harness (evals/shared_backend_concurrency/harness.py --freeze --node-dir ...) passes a
// target: a throwaway `hermes serve` whose whole process tree it has suspended (the 09-07 signature).
// The supervisor's decision runs as in production with its clock scaled down (3 s instead of 120 s),
// dumps are written by the real `python -m hermes_cli.freeze_dump`, and the kill is a real
// `taskkill /T /F` of that exact PID. The respawn afterwards is main.ts's exit recovery (not run here).

import assert from 'node:assert/strict'
import { execFile, execFileSync } from 'node:child_process'
import fs from 'node:fs'

import { test } from 'vitest'

import { createBackendLivenessSupervisor } from './backend-liveness'

interface E2eTarget {
  pid: number
  baseUrl: string
  python: string
  repo: string
  dumpDir: string
  resultPath: string
}

const targetJson = process.env.LIVENESS_E2E_TARGET

function alive(pid: number): boolean {
  try {
    process.kill(pid, 0)

    return true
  } catch (error) {
    return (error as NodeJS.ErrnoException).code === 'EPERM'
  }
}

test.skipIf(!targetJson)('the supervisor dumps, then kills, a real frozen backend', async () => {
  const target = JSON.parse(targetJson!) as E2eTarget
  const events: string[] = []
  const started = Date.now()

  await new Promise<void>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('the supervisor never acted on the frozen backend')), 120_000)

    const supervisor = createBackendLivenessSupervisor({
      intervalMs: 500,
      isAlive: alive,
      log: line => void events.push(line),
      probeTimeoutMs: 1_000,
      unresponsiveMs: 3_000
    })

    supervisor.watch('primary', {
      baseUrl: target.baseUrl,
      captureDumps: () =>
        new Promise(done => {
          events.push(`dump:start:${alive(target.pid) ? 'alive' : 'dead'}`)
          execFile(
            target.python,
            ['-m', 'hermes_cli.freeze_dump', String(target.pid), target.dumpDir, '--label', 'e2e'],
            { cwd: target.repo, timeout: 90_000, windowsHide: true },
            (error, stdout) => {
              events.push(error ? `dump:failed:${error.message}` : `dump:written:${String(stdout).trim().split(/\r?\n/).length}`)
              done()
            }
          )
        }),
      kill: () => {
        events.push('kill')
        execFileSync('taskkill', ['/PID', String(target.pid), '/T', '/F'], { stdio: 'ignore', windowsHide: true })
        clearTimeout(timer)
        resolve()
      },
      pid: target.pid
    })
  })

  const deadline = Date.now() + 15_000

  while (alive(target.pid) && Date.now() < deadline) {
    await new Promise(resolve => setTimeout(resolve, 200))
  }

  const result = { events, killed: !alive(target.pid), actedAfterMs: Date.now() - started }
  fs.writeFileSync(target.resultPath, JSON.stringify(result, null, 2))
  assert.equal(result.killed, true, 'the frozen backend survived the supervisor')
  assert.ok(events.indexOf('dump:start:alive') < events.indexOf('kill'), 'dumps must be taken BEFORE the kill')
  assert.ok(events.some(e => e.startsWith('dump:written:')), 'the native dumps were not written')
}, 180_000)
