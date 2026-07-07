#!/usr/bin/env node
// Task 13 — manual local stress harness for Desktop same-profile session backends.
//
// SIMULATED / TEST-MODE BY DEFAULT. This does NOT launch a real Desktop process,
// open sockets, or call any model/provider API — it drives the real
// DesktopBackendPool from apps/desktop/electron/backend-pool.cjs with FAKE spawn /
// connection / process objects so a developer can reproduce the target scenario
// locally with zero cost and zero side effects:
//
//   10 claudetriad session backends → a small "prompt" to each → one backend
//   killed mid-run → assert the remaining 9 still health-check and keep their
//   exact descriptor/connection/process identities → the killed scope respawns
//   with a brand-new process and nothing else is disturbed.
//
// Usage:
//   node scripts/stress_desktop_session_backends.mjs [options]
//
// Options:
//   --profile <name>     Profile for all sessions          (default: claudetriad)
//   --sessions <n>       Number of session backends        (default: 10)
//   --kill-index <i>     Which session (0-based) to kill   (default: 0)
//   --no-respawn-killed  Skip re-ensuring the killed scope (default: respawn it)
//   --json               Emit a machine-readable JSON result on stdout
//   --mode <sim|real>    'sim' (default). 'real' is NOT implemented and errors.
//   -h, --help           Show this help.
//
// Exits 0 when every invariant holds; nonzero with diagnostics otherwise.

import { createRequire } from 'node:module'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const require = createRequire(import.meta.url)
const __dirname = path.dirname(fileURLToPath(import.meta.url))
const POOL_PATH = path.join(__dirname, '..', 'apps', 'desktop', 'electron', 'backend-pool.cjs')
const { DesktopBackendPool } = require(POOL_PATH)

// ── CLI parsing (no deps) ─────────────────────────────────────────────────
function parseArgs(argv) {
  const opts = {
    profile: 'claudetriad',
    sessions: 10,
    killIndex: 0,
    respawnKilled: true,
    json: false,
    mode: 'sim',
    help: false
  }
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i]
    switch (a) {
      case '-h':
      case '--help': opts.help = true; break
      case '--json': opts.json = true; break
      case '--no-respawn-killed': opts.respawnKilled = false; break
      case '--profile': opts.profile = argv[++i]; break
      case '--sessions': opts.sessions = Number(argv[++i]); break
      case '--kill-index': opts.killIndex = Number(argv[++i]); break
      case '--mode': opts.mode = argv[++i]; break
      default:
        throw new Error(`unknown argument: ${a} (use --help)`)
    }
  }
  return opts
}

const HELP = `stress_desktop_session_backends.mjs — SIMULATED Desktop session-backend stress (test-mode, no API/network)

  node scripts/stress_desktop_session_backends.mjs [--profile claudetriad] [--sessions 10]
      [--kill-index 0] [--no-respawn-killed] [--json] [--mode sim]

Drives the real DesktopBackendPool with fake spawn/connection/process objects:
creates N session backends for one profile, sends a local-only prompt to each,
kills one mid-run, and asserts the remaining N-1 are untouched and still healthy.
No real model/provider calls, no sockets, no Desktop process. '--mode real' errors.`

function fail(msg) {
  const e = new Error(msg)
  e.invariant = true
  return e
}

function usageError(msg) {
  const e = new Error(msg)
  e.usage = true
  return e
}

function assert(cond, label) {
  if (!cond) throw fail(`INVARIANT FAILED: ${label}`)
  return label
}

// ── Fake substrate (deterministic, local-only) ───────────────────────────
function makeFakeProcess(pid) {
  return {
    pid,
    killed: false,
    alive: true,
    signals: [],
    kill(signal = 'SIGTERM') {
      this.killed = true
      this.alive = false
      this.signals.push(signal)
      return true
    }
  }
}

function run(opts) {
  if (opts.mode === 'real') {
    const e = new Error(
      "--mode real is NOT implemented: this harness is simulation-only to avoid " +
      "real model/API calls and Desktop side effects. Use the default --mode sim."
    )
    e.notImplemented = true
    throw e
  }
  if (opts.mode !== 'sim') throw usageError(`unknown --mode: ${opts.mode} (expected 'sim' or 'real')`)
  if (!Number.isInteger(opts.sessions) || opts.sessions < 1) throw usageError('--sessions must be a positive integer')
  if (!Number.isInteger(opts.killIndex) || opts.killIndex < 0 || opts.killIndex >= opts.sessions) {
    throw usageError(`--kill-index must be in [0, ${opts.sessions - 1}]`)
  }

  const checks = []
  const record = label => checks.push(label)

  const spawned = []
  const stopped = []
  const probes = []
  let primaryStartCount = 0
  let pidSeq = 30000

  const pool = new DesktopBackendPool({
    // A primary key that never matches our profile; session scopes never route to
    // the primary anyway (their keys carry the `session:` prefix).
    primaryProfileKey: () => 'default',
    startPrimary: async () => {
      primaryStartCount += 1
      throw new Error('startPrimary must NOT be called by a session-scope stress run')
    },
    spawnBackend: async (key, entry) => {
      const proc = makeFakeProcess(pidSeq++)
      entry.process = proc
      spawned.push({ key, pid: proc.pid, scope: entry.scope, profile: entry.profile, sessionId: entry.sessionId })
      const connection = {
        key,
        baseUrl: `sim://127.0.0.1/${key}/${proc.pid}`,
        token: `tok-${proc.pid}`,
        process: proc,
        turns: 0,
        // Local-only "turn": echoes the prompt, counts the turn. NEVER hits a provider.
        sendPrompt(prompt) {
          if (!this.process.alive) throw new Error(`sendPrompt on a dead backend ${key}`)
          this.turns += 1
          return { ok: true, key, echo: String(prompt), turns: this.turns }
        }
      }
      return connection
    },
    // Healthy iff the backing fake process is still alive. Records the timeout opt.
    healthProbe: async (_conn, entry, probeOpts) => {
      probes.push({ key: `session:${entry.profile}:${entry.sessionId}`, timeoutMs: probeOpts?.timeoutMs })
      return entry.process?.alive === true
    },
    stopEntry: (key, entry) => {
      stopped.push(key)
      if (entry?.process && !entry.process.killed) entry.process.kill('SIGTERM')
    },
    waitForBackendExit: async proc => (proc ? !proc.alive : true),
    setIntervalFn: () => ({ unref() {} }),
    clearIntervalFn: () => {}
  })

  const { profile, sessions: N, killIndex } = opts
  const sessionIds = Array.from({ length: N }, (_, i) => `s${i}`)
  const keyOf = sid => `session:${profile}:${sid}`

  return (async () => {
    // 1) Create N session backends.
    const conns = new Map()
    for (const sid of sessionIds) {
      conns.set(sid, await pool.ensureBackend({ profile, sessionId: sid, isolation: 'session' }))
    }
    record(assert(pool.size === N, `created exactly ${N} pool entries`))
    const expectedKeys = new Set(sessionIds.map(keyOf))
    record(assert(setsEqual(new Set([...pool.keys()]), expectedKeys), `pool keys are exactly session:${profile}:s0..s${N - 1}`))
    record(assert(primaryStartCount === 0, 'no primary backend was started'))
    record(assert(![...pool.keys()].includes(profile), 'no bare profile-scope backend was created'))
    record(assert(spawned.every(s => s.scope === 'session'), 'every spawn was session-scoped'))

    // 2) Send a small local prompt to each backend.
    for (const sid of sessionIds) {
      const res = conns.get(sid).sendPrompt(`hello from ${sid}`)
      assert(res.ok === true && res.key === keyOf(sid), `prompt echoed for ${sid}`)
    }
    record(assert([...conns.values()].every(c => c.turns === 1), 'all initial prompts succeeded locally'))

    // 3) Snapshot identities BEFORE the kill.
    const snap = new Map()
    for (const sid of sessionIds) {
      const entry = pool.get(keyOf(sid))
      snap.set(sid, { entry, connection: entry.connection, process: entry.process, pid: entry.process.pid })
    }

    // 4) Kill ONE backend mid-run via its fake process kill path.
    const killedSid = sessionIds[killIndex]
    const killedProc = snap.get(killedSid).process
    killedProc.kill('SIGKILL')
    record(assert(killedProc.killed === true, `killed backend ${keyOf(killedSid)} process marked killed`))
    record(assert(killedProc.signals.length === 1, 'killed process received exactly one signal'))

    // 5) Remaining N-1: still present, still healthy, identities strictly unchanged.
    const survivors = sessionIds.filter(sid => sid !== killedSid)
    for (const sid of survivors) {
      const s = snap.get(sid)
      assert(pool.has(keyOf(sid)), `survivor ${sid} still exists`)
      assert(pool.get(keyOf(sid)) === s.entry, `survivor ${sid} entry identity unchanged`)
      assert(s.entry.connection === s.connection, `survivor ${sid} connection identity unchanged`)
      assert(s.entry.process === s.process, `survivor ${sid} process identity unchanged`)
      assert(s.process.alive === true && s.process.killed === false, `survivor ${sid} process still alive`)
      assert((await pool.isHealthy(s.entry.connection, s.entry)) === true, `survivor ${sid} health check passes`)
    }
    record(assert(true, `all ${survivors.length} survivors present, healthy, and identity-stable`))
    // The kill must not have signaled anyone else.
    record(assert(survivors.every(sid => snap.get(sid).process.signals.length === 0), 'no survivor process was signaled'))

    // 6) Optionally respawn the killed scope and prove ONLY it changed.
    let respawn = null
    if (opts.respawnKilled) {
      const before = snap.get(killedSid)
      await pool.ensureBackend({ profile, sessionId: killedSid, isolation: 'session' })
      const after = pool.get(keyOf(killedSid))
      record(assert(after !== before.entry, `killed ${killedSid} got a NEW entry on respawn`))
      record(assert(after.connection !== before.connection, `killed ${killedSid} got a NEW connection`))
      record(assert(after.process !== before.process, `killed ${killedSid} got a NEW process`))
      record(assert(after.process.pid !== before.pid, `killed ${killedSid} got a NEW pid`))
      record(assert(stopped.includes(keyOf(killedSid)), `killed ${killedSid} was torn down via its own key`))
      record(assert(stopped.filter(k => k === keyOf(killedSid)).length === 1 && stopped.every(k => k === keyOf(killedSid)),
        'only the killed scope was ever torn down'))
      // Survivors remain byte-for-byte identical AFTER the respawn too.
      for (const sid of survivors) {
        const s = snap.get(sid)
        assert(pool.get(keyOf(sid)) === s.entry && s.entry.process === s.process,
          `survivor ${sid} untouched by killed-scope respawn`)
      }
      record(assert(pool.size === N, `pool still holds exactly ${N} entries after respawn`))
      record(assert(setsEqual(new Set([...pool.keys()]), expectedKeys), 'pool key set unchanged after respawn'))
      respawn = { session: killedSid, oldPid: before.pid, newPid: after.process.pid }
    } else {
      record(assert(pool.has(keyOf(killedSid)), 'killed scope entry retained (no respawn requested)'))
    }

    return {
      ok: true,
      mode: 'sim',
      profile,
      sessions: N,
      killIndex,
      killedSession: killedSid,
      respawnedKilled: Boolean(opts.respawnKilled),
      respawn,
      spawnCount: spawned.length,
      stopped: [...stopped],
      poolKeys: [...pool.keys()],
      probeTimeoutMs: pool.settings.healthTimeoutMs,
      checksPassed: checks.length,
      checks
    }
  })()
}

function setsEqual(a, b) {
  if (a.size !== b.size) return false
  for (const x of a) if (!b.has(x)) return false
  return true
}

// ── Entrypoint ────────────────────────────────────────────────────────────
async function main() {
  let opts
  try {
    opts = parseArgs(process.argv.slice(2))
  } catch (err) {
    process.stderr.write(`${err.message}\n`)
    process.exit(2)
  }
  if (opts.help) {
    process.stdout.write(`${HELP}\n`)
    process.exit(0)
  }

  try {
    const result = await run(opts)
    if (opts.json) {
      process.stdout.write(`${JSON.stringify(result, null, 2)}\n`)
    } else {
      const lines = [
        `✔ SIMULATED Desktop session-backend stress passed (no API/network/Desktop)`,
        `  profile=${result.profile} sessions=${result.sessions} killed=${result.killedSession} (index ${result.killIndex})`,
        `  spawns=${result.spawnCount} torn-down=${JSON.stringify(result.stopped)} pool=${result.sessions} entries`,
        result.respawn
          ? `  respawn: ${result.respawn.session} pid ${result.respawn.oldPid} → ${result.respawn.newPid} (only this scope changed)`
          : `  respawn: skipped (--no-respawn-killed)`,
        `  invariants checked: ${result.checksPassed}`
      ]
      process.stdout.write(`${lines.join('\n')}\n`)
    }
    process.exit(0)
  } catch (err) {
    if (opts.json) {
      process.stdout.write(`${JSON.stringify({ ok: false, error: err.message, invariant: Boolean(err.invariant), notImplemented: Boolean(err.notImplemented), usage: Boolean(err.usage) }, null, 2)}\n`)
    } else {
      process.stderr.write(`✘ stress FAILED: ${err.message}\n`)
      // Only dump a stack for genuinely unexpected errors — usage/invariant/
      // not-implemented failures are self-explanatory from the message.
      if (err.stack && !err.invariant && !err.notImplemented && !err.usage) process.stderr.write(`${err.stack}\n`)
    }
    process.exit(1)
  }
}

main()
