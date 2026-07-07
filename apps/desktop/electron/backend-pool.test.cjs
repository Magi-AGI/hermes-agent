const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')

const {
  DesktopBackendPool,
  backendScopeForTarget,
  backendScopeKey,
  normalizeManagementScope,
  readBackendPoolSettingsFromConfig,
  resolveBackendPoolSettings,
  resolveBackendTarget
} = require('./backend-pool.cjs')

function fakeProcess(pid) {
  return {
    pid,
    killed: false,
    signals: [],
    kill(signal) {
      this.killed = true
      this.signals.push(signal)
    }
  }
}

test('pool settings resolve from config with sane defaults and no env requirement', () => {
  assert.deepEqual(resolveBackendPoolSettings({}), {
    maxProfileBackends: 3,
    maxSessionBackendsPerProfile: 10,
    spawnConcurrency: 10,
    idleMs: 10 * 60_000,
    keepaliveFreshMs: 90_000,
    healthTimeoutMs: 2_500,
    startupTimeoutMs: 90_000,
    healthRecheckDelayMs: 300,
    respawnBreakerMax: 5,
    respawnBreakerWindowMs: 60_000,
    autoReapOrphans: true,
    backendIsolation: 'profile'
  })

  assert.deepEqual(
    resolveBackendPoolSettings({
      desktop: {
        backend_pool: {
          max_profile_backends: 8,
          idle_ms: 3_600_000,
          health_timeout_ms: 1_000,
          keepalive_fresh_ms: 120_000,
          auto_reap_orphans: false
        }
      }
    }),
    {
      maxProfileBackends: 8,
      maxSessionBackendsPerProfile: 10,
      spawnConcurrency: 10,
      idleMs: 3_600_000,
      keepaliveFreshMs: 120_000,
      healthTimeoutMs: 1_000,
      startupTimeoutMs: 90_000,
      healthRecheckDelayMs: 300,
      respawnBreakerMax: 5,
      respawnBreakerWindowMs: 60_000,
      autoReapOrphans: false,
      backendIsolation: 'profile'
    }
  )
})

test('backend_isolation parses profile|hybrid|session and falls back to profile', () => {
  assert.equal(resolveBackendPoolSettings({}).backendIsolation, 'profile')
  assert.equal(
    resolveBackendPoolSettings({ desktop: { backend_pool: { backend_isolation: 'hybrid' } } }).backendIsolation,
    'hybrid'
  )
  assert.equal(
    resolveBackendPoolSettings({ desktop: { backend_pool: { backend_isolation: 'SESSION' } } }).backendIsolation,
    'session'
  )
  // Invalid / unknown values fall back to profile.
  assert.equal(
    resolveBackendPoolSettings({ desktop: { backend_pool: { backend_isolation: 'bogus' } } }).backendIsolation,
    'profile'
  )
  assert.equal(
    resolveBackendPoolSettings({ desktop: { backend_pool: { backend_isolation: '' } } }).backendIsolation,
    'profile'
  )
})

test('backend_isolation is read from config.yaml desktop.backend_pool', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-backend-iso-test-'))
  const configPath = path.join(dir, 'config.yaml')
  fs.writeFileSync(
    configPath,
    ['desktop:', '  backend_pool:', '    backend_isolation: hybrid', ''].join('\n')
  )
  assert.equal(readBackendPoolSettingsFromConfig(configPath).backendIsolation, 'hybrid')
})

test('pool settings can be read from config.yaml desktop.backend_pool scalars', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-backend-pool-config-test-'))
  const configPath = path.join(dir, 'config.yaml')
  fs.writeFileSync(
    configPath,
    [
      'model: test-model',
      'desktop:',
      '  backend_pool:',
      '    max_profile_backends: 8',
      '    max_session_backends_per_profile: 10',
      '    spawn_concurrency: 10',
      '    idle_ms: 3600000',
      '    health_timeout_ms: 1250',
      '    startup_timeout_ms: 91000',
      '    keepalive_fresh_ms: 123000',
      '    auto_reap_orphans: false',
      ''
    ].join('\n')
  )

  assert.equal(readBackendPoolSettingsFromConfig(configPath).maxProfileBackends, 8)
  assert.equal(readBackendPoolSettingsFromConfig(configPath).idleMs, 3_600_000)
  assert.equal(readBackendPoolSettingsFromConfig(configPath).healthTimeoutMs, 1_250)
  assert.equal(readBackendPoolSettingsFromConfig(configPath).autoReapOrphans, false)
  assert.deepEqual(readBackendPoolSettingsFromConfig(path.join(dir, 'missing.yaml')), resolveBackendPoolSettings({}))
})

test('backend_isolation is read from the canonical top-level desktop.backend_isolation', () => {
  // Canonical top-level key.
  assert.equal(
    resolveBackendPoolSettings({ desktop: { backend_isolation: 'hybrid' } }).backendIsolation,
    'hybrid'
  )
  assert.equal(
    resolveBackendPoolSettings({ desktop: { backend_isolation: 'SESSION' } }).backendIsolation,
    'session'
  )
  // Invalid top-level falls back to profile (does NOT fall through to nested).
  assert.equal(
    resolveBackendPoolSettings({
      desktop: { backend_isolation: 'bogus', backend_pool: { backend_isolation: 'session' } }
    }).backendIsolation,
    'profile'
  )
})

test('top-level desktop.backend_isolation wins over the nested backend_pool alias', () => {
  assert.equal(
    resolveBackendPoolSettings({
      desktop: { backend_isolation: 'hybrid', backend_pool: { backend_isolation: 'session' } }
    }).backendIsolation,
    'hybrid'
  )
  // A null/blank top-level is unset → the nested alias is used.
  assert.equal(
    resolveBackendPoolSettings({
      desktop: { backend_isolation: null, backend_pool: { backend_isolation: 'session' } }
    }).backendIsolation,
    'session'
  )
  assert.equal(
    resolveBackendPoolSettings({
      desktop: { backend_isolation: '  ', backend_pool: { backend_isolation: 'session' } }
    }).backendIsolation,
    'session'
  )
})

test('readBackendPoolSettingsFromConfig reads canonical top-level isolation + pool scalars', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-backend-iso-top-test-'))
  const configPath = path.join(dir, 'config.yaml')
  fs.writeFileSync(
    configPath,
    [
      'desktop:',
      '  backend_isolation: hybrid',
      '  backend_pool:',
      '    max_session_backends_per_profile: 10',
      ''
    ].join('\n')
  )
  const settings = readBackendPoolSettingsFromConfig(configPath)
  assert.equal(settings.backendIsolation, 'hybrid')
  assert.equal(settings.maxSessionBackendsPerProfile, 10)
})

test('readBackendPoolSettingsFromConfig: top-level isolation wins over nested in YAML', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-backend-iso-both-test-'))
  const configPath = path.join(dir, 'config.yaml')
  fs.writeFileSync(
    configPath,
    ['desktop:', '  backend_isolation: session', '  backend_pool:', '    backend_isolation: hybrid', ''].join('\n')
  )
  assert.equal(readBackendPoolSettingsFromConfig(configPath).backendIsolation, 'session')
})

test('blank / null desktop config falls back to defaults without throwing', () => {
  assert.equal(resolveBackendPoolSettings({ desktop: null }).backendIsolation, 'profile')
  assert.equal(resolveBackendPoolSettings({ desktop: { backend_pool: null } }).maxSessionBackendsPerProfile, 10)
  assert.deepEqual(resolveBackendPoolSettings({ desktop: null }), resolveBackendPoolSettings({}))
})

test('primary profile routes to primary starter and empty profile is primary', async () => {
  let primaryCalls = 0
  const pool = new DesktopBackendPool({
    primaryProfileKey: () => 'default',
    startPrimary: async () => {
      primaryCalls += 1
      return { mode: 'primary' }
    },
    spawnBackend: async () => {
      throw new Error('should not spawn')
    }
  })

  assert.deepEqual(await pool.ensureBackend(''), { mode: 'primary' })
  assert.deepEqual(await pool.ensureBackend('default'), { mode: 'primary' })
  assert.equal(primaryCalls, 2)
  assert.equal(pool.size, 0)
})

test('non-primary profile spawns once and healthy cached reuse returns same connection', async () => {
  let now = 1000
  let spawnCalls = 0
  let healthCalls = 0
  const child = fakeProcess(42)
  const pool = new DesktopBackendPool({
    primaryProfileKey: () => 'default',
    now: () => now,
    startPrimary: async () => ({ mode: 'primary' }),
    spawnBackend: async (profile, entry) => {
      spawnCalls += 1
      entry.process = child
      return { profile, baseUrl: 'http://127.0.0.1:1', token: 't1', process: child }
    },
    healthProbe: async conn => {
      healthCalls += 1
      assert.equal(conn.baseUrl, 'http://127.0.0.1:1')
      return true
    },
    setIntervalFn: () => ({ unref() {} }),
    clearIntervalFn: () => {}
  })

  const first = await pool.ensureBackend('claudetriad')
  now = 2000
  const second = await pool.ensureBackend('claudetriad')

  assert.equal(first, second)
  assert.equal(spawnCalls, 1)
  assert.equal(healthCalls, 1)
  assert.equal(pool.get('claudetriad').lastActiveAt, 2000)
})

test('cached backend failing health probe is stopped and respawned', async () => {
  let spawnCalls = 0
  const children = [fakeProcess(1), fakeProcess(2)]
  const pool = new DesktopBackendPool({
    primaryProfileKey: () => 'default',
    startPrimary: async () => ({ mode: 'primary' }),
    spawnBackend: async (profile, entry) => {
      const child = children[spawnCalls]
      spawnCalls += 1
      entry.process = child
      return { profile, baseUrl: `http://127.0.0.1:${spawnCalls}`, token: `t${spawnCalls}` }
    },
    healthProbe: async () => false,
    setIntervalFn: () => ({ unref() {} }),
    clearIntervalFn: () => {}
  })

  const first = await pool.ensureBackend('claudetriad')
  const second = await pool.ensureBackend('claudetriad')

  assert.notEqual(first, second)
  assert.equal(spawnCalls, 2)
  assert.deepEqual(children[0].signals, ['SIGTERM'])
  assert.equal(children[1].killed, false)
})

test('spawn failure removes failed entry from pool', async () => {
  const pool = new DesktopBackendPool({
    primaryProfileKey: () => 'default',
    startPrimary: async () => ({ mode: 'primary' }),
    spawnBackend: async () => {
      throw new Error('boom')
    },
    setIntervalFn: () => ({ unref() {} }),
    clearIntervalFn: () => {}
  })

  await assert.rejects(pool.ensureBackend('broken'), /boom/)
  assert.equal(pool.get('broken'), undefined)
  assert.equal(pool.size, 0)
})

test('spawn failure removes failed entry from pool and cleans up half-started process', async () => {
  const child = fakeProcess(500)
  const cleaned = []
  const pool = new DesktopBackendPool({
    primaryProfileKey: () => 'default',
    startPrimary: async () => ({ mode: 'primary' }),
    spawnBackend: async (profile, entry) => {
      entry.process = child
      entry.registryId = `profile:${profile}`
      throw new Error('boom')
    },
    stopEntry: (profile, entry) => {
      cleaned.push({ profile, registryId: entry.registryId })
      if (entry.process) entry.process.kill('SIGTERM')
    },
    setIntervalFn: () => ({ unref() {} }),
    clearIntervalFn: () => {}
  })

  await assert.rejects(() => pool.ensureBackend('claudetriad'), /boom/)
  assert.equal(pool.size, 0)
  assert.equal(child.killed, true)
  assert.deepEqual(cleaned, [{ profile: 'claudetriad', registryId: 'profile:claudetriad' }])
})

test('LRU eviction only stops stale entries and spares fresh kept-alive entries', async () => {
  let now = 1_000_000
  const stopped = []
  const pool = new DesktopBackendPool({
    settings: { maxProfileBackends: 3, keepaliveFreshMs: 90_000 },
    primaryProfileKey: () => 'default',
    now: () => now,
    startPrimary: async () => ({ mode: 'primary' }),
    spawnBackend: async (profile, entry) => {
      entry.process = fakeProcess(profile.charCodeAt(0))
      return { profile, baseUrl: `http://127.0.0.1/${profile}`, token: profile }
    },
    stopEntry: (profile, entry) => {
      stopped.push(profile)
      entry.process.kill('SIGTERM')
    },
    setIntervalFn: () => ({ unref() {} }),
    clearIntervalFn: () => {}
  })

  await pool.ensureBackend('a')
  pool.get('a').lastActiveAt = now - 200_000
  await pool.ensureBackend('b')
  pool.get('b').lastActiveAt = now - 150_000
  await pool.ensureBackend('c')
  pool.get('c').lastActiveAt = now - 1_000
  await pool.ensureBackend('d')

  assert.deepEqual(stopped, ['a'])
  assert.equal(pool.has('b'), true)
  assert.equal(pool.has('c'), true)
  assert.equal(pool.has('d'), true)
})

test('idle reaper stops idle entries and clears timer when pool becomes empty', async () => {
  let now = 500_000
  let intervalCallback = null
  let cleared = false
  const pool = new DesktopBackendPool({
    settings: { idleMs: 60_000 },
    primaryProfileKey: () => 'default',
    now: () => now,
    startPrimary: async () => ({ mode: 'primary' }),
    spawnBackend: async (profile, entry) => {
      entry.process = fakeProcess(100)
      return { profile, baseUrl: 'http://127.0.0.1', token: profile }
    },
    setIntervalFn: callback => {
      intervalCallback = callback
      return { unref() {} }
    },
    clearIntervalFn: () => {
      cleared = true
    }
  })

  await pool.ensureBackend('claudetriad')
  pool.get('claudetriad').lastActiveAt = now - 120_000
  intervalCallback()

  assert.equal(pool.size, 0)
  assert.equal(cleared, true)
})

test('child-exit cleanup can forget an entry without signaling it again', async () => {
  const child = fakeProcess(654)
  const forgotten = []
  let stopCalls = 0
  const pool = new DesktopBackendPool({
    primaryProfileKey: () => 'default',
    startPrimary: async () => ({ mode: 'primary' }),
    spawnBackend: async (profile, entry) => {
      entry.process = child
      entry.registryId = `profile:${profile}`
      entry.sidecarPath = `/tmp/${profile}.json`
      return { profile, baseUrl: 'http://127.0.0.1', token: profile }
    },
    stopEntry: () => {
      stopCalls += 1
    },
    forgetEntry: (profile, entry) => {
      forgotten.push({ profile, registryId: entry.registryId, sidecarPath: entry.sidecarPath })
    },
    setIntervalFn: () => ({ unref() {} }),
    clearIntervalFn: () => {}
  })

  await pool.ensureBackend('claudetriad')
  assert.equal(pool.forgetBackend('claudetriad'), true)
  assert.equal(pool.size, 0)
  assert.equal(child.killed, false)
  assert.equal(stopCalls, 0)
  assert.deepEqual(forgotten, [{ profile: 'claudetriad', registryId: 'profile:claudetriad', sidecarPath: '/tmp/claudetriad.json' }])
})

test('stopAllBackends stops every pooled backend through the public main-process alias', async () => {
  const stopped = []
  const pool = new DesktopBackendPool({
    primaryProfileKey: () => 'default',
    startPrimary: async () => ({ mode: 'primary' }),
    spawnBackend: async (profile, entry) => {
      entry.process = fakeProcess(profile.length)
      return { profile, baseUrl: `http://127.0.0.1/${profile}`, token: profile }
    },
    stopEntry: profile => stopped.push(profile),
    setIntervalFn: () => ({ unref() {} }),
    clearIntervalFn: () => {}
  })

  await pool.ensureBackend('alpha')
  await pool.ensureBackend('beta')
  pool.stopAllBackends()
  assert.deepEqual(stopped.sort(), ['alpha', 'beta'])
  assert.equal(pool.size, 0)
})

test('pool exposes child PIDs and async teardown waits for backend exit hook', async () => {
  const waited = []
  const child = fakeProcess(321)
  const pool = new DesktopBackendPool({
    primaryProfileKey: () => 'default',
    startPrimary: async () => ({ mode: 'primary' }),
    spawnBackend: async (profile, entry) => {
      entry.process = child
      return { profile, baseUrl: 'http://127.0.0.1', token: profile }
    },
    waitForBackendExit: async proc => waited.push(proc.pid),
    setIntervalFn: () => ({ unref() {} }),
    clearIntervalFn: () => {}
  })

  await pool.ensureBackend('claudetriad')
  assert.deepEqual(pool.childPids(), [321])
  await pool.teardownBackendAndWait('claudetriad')
  assert.deepEqual(child.signals, ['SIGTERM'])
  assert.deepEqual(waited, [321])
  assert.equal(pool.size, 0)
})

test('teardown signals first and does NOT tombstone when exit is confirmed', async () => {
  const order = []
  const child = fakeProcess(700)
  const pool = new DesktopBackendPool({
    primaryProfileKey: () => 'default',
    startPrimary: async () => ({ mode: 'primary' }),
    spawnBackend: async (profile, entry) => {
      entry.process = child
      entry.registryId = `profile:${profile}`
      return { profile, baseUrl: 'http://127.0.0.1', token: profile }
    },
    stopEntry: () => order.push('signal'),
    waitForBackendExit: async () => {
      order.push('wait')
      return true
    },
    tombstoneEntry: () => order.push('tombstone'),
    setIntervalFn: () => ({ unref() {} }),
    clearIntervalFn: () => {}
  })

  await pool.ensureBackend('claudetriad')
  await pool.teardownBackendAndWait('claudetriad')

  // Signal BEFORE waiting for exit; no tombstone on a confirmed exit.
  assert.deepEqual(order, ['signal', 'wait'])
  assert.equal(pool.size, 0)
})

test('health-probe-failed restart refuses to respawn when old exit is unconfirmed and tombstones it', async () => {
  let spawnCalls = 0
  const child = fakeProcess(900)
  const tombstoned = []
  const pool = new DesktopBackendPool({
    primaryProfileKey: () => 'default',
    startPrimary: async () => ({ mode: 'primary' }),
    spawnBackend: async (profile, entry) => {
      spawnCalls += 1
      entry.process = child
      entry.registryId = `profile:${profile}`
      entry.ownerNonce = `nonce-${spawnCalls}`
      return { profile, baseUrl: 'http://127.0.0.1', token: 't', process: child }
    },
    healthProbe: async () => false, // cached backend is unhealthy
    waitForBackendExit: async () => false, // hung: exit cannot be confirmed
    tombstoneEntry: (profile, entry) => tombstoned.push({ profile, lifecycle: entry.lifecycle }),
    sleep: async () => {}, // instant re-probe delay
    setIntervalFn: () => ({ unref() {} }),
    clearIntervalFn: () => {}
  })

  await pool.ensureBackend('claudetriad') // spawn #1
  await assert.rejects(() => pool.ensureBackend('claudetriad'), /unresponsive|could not be confirmed/)

  assert.equal(spawnCalls, 1, 'no same-id replacement was spawned over the wedged old generation')
  assert.deepEqual(tombstoned, [{ profile: 'claudetriad', lifecycle: 'failed' }])
  assert.equal(pool.size, 0)
})

test('idle reap tombstones a backend whose exit cannot be confirmed', async () => {
  let now = 500_000
  let intervalCallback = null
  const tombstoned = []
  const pool = new DesktopBackendPool({
    settings: { idleMs: 60_000 },
    primaryProfileKey: () => 'default',
    now: () => now,
    startPrimary: async () => ({ mode: 'primary' }),
    spawnBackend: async (profile, entry) => {
      entry.process = fakeProcess(100)
      entry.registryId = `profile:${profile}`
      return { profile, baseUrl: 'http://127.0.0.1', token: profile }
    },
    waitForBackendExit: async () => false,
    tombstoneEntry: (profile, entry) => tombstoned.push({ profile, lifecycle: entry.lifecycle }),
    setIntervalFn: callback => {
      intervalCallback = callback
      return { unref() {} }
    },
    clearIntervalFn: () => {}
  })

  await pool.ensureBackend('claudetriad')
  pool.get('claudetriad').lastActiveAt = now - 120_000
  intervalCallback() // reapIdle
  await new Promise(resolve => setImmediate(resolve)) // let the background teardown settle

  assert.deepEqual(tombstoned, [{ profile: 'claudetriad', lifecycle: 'failed' }])
  assert.equal(pool.size, 0)
})

test('LRU eviction tombstones an evicted backend whose exit cannot be confirmed', async () => {
  let now = 1_000_000
  const tombstoned = []
  const pool = new DesktopBackendPool({
    settings: { maxProfileBackends: 1, keepaliveFreshMs: 90_000 },
    primaryProfileKey: () => 'default',
    now: () => now,
    startPrimary: async () => ({ mode: 'primary' }),
    spawnBackend: async (profile, entry) => {
      entry.process = fakeProcess(profile.charCodeAt(0))
      entry.registryId = `profile:${profile}`
      return { profile, baseUrl: `http://127.0.0.1/${profile}`, token: profile }
    },
    waitForBackendExit: async () => false,
    tombstoneEntry: profile => tombstoned.push(profile),
    setIntervalFn: () => ({ unref() {} }),
    clearIntervalFn: () => {}
  })

  await pool.ensureBackend('a')
  pool.get('a').lastActiveAt = now - 200_000 // stale enough to evict
  await pool.ensureBackend('b') // cap 1 → evicts 'a'
  await new Promise(resolve => setImmediate(resolve))

  assert.deepEqual(tombstoned, ['a'])
  assert.equal(pool.has('b'), true)
})

test('teardown tombstones (retains record) when exit cannot be confirmed', async () => {
  const child = fakeProcess(701)
  const tombstoned = []
  const pool = new DesktopBackendPool({
    primaryProfileKey: () => 'default',
    startPrimary: async () => ({ mode: 'primary' }),
    spawnBackend: async (profile, entry) => {
      entry.process = child
      entry.registryId = `profile:${profile}`
      entry.ownerNonce = 'nonce-1'
      return { profile, baseUrl: 'http://127.0.0.1', token: profile }
    },
    stopEntry: () => {},
    waitForBackendExit: async () => false, // force-kill did not confirm exit
    tombstoneEntry: (profile, entry) => tombstoned.push({ profile, registryId: entry.registryId, lifecycle: entry.lifecycle }),
    setIntervalFn: () => ({ unref() {} }),
    clearIntervalFn: () => {}
  })

  await pool.ensureBackend('claudetriad')
  await pool.teardownBackendAndWait('claudetriad')

  assert.deepEqual(tombstoned, [{ profile: 'claudetriad', registryId: 'profile:claudetriad', lifecycle: 'failed' }])
  assert.equal(pool.size, 0)
})

// ── M4a: session-scoped backend descriptors ──────────────────────────────

test('backendScopeKey: profile keys stable; session key is session:<profile>:<sessionId>', () => {
  assert.equal(backendScopeKey({ profile: 'claudetriad' }), 'claudetriad')
  assert.equal(backendScopeKey({ profile: null }), 'default')
  assert.equal(backendScopeKey({}), 'default')
  assert.equal(backendScopeKey({ profile: 'claudetriad', sessionId: 'A', isolation: 'session' }), 'session:claudetriad:A')
  const a = backendScopeKey({ profile: 'claudetriad', sessionId: 'A', isolation: 'session' })
  const b = backendScopeKey({ profile: 'claudetriad', sessionId: 'B', isolation: 'session' })
  assert.notEqual(a, b)
  // A sessionId WITHOUT explicit session isolation stays profile scope.
  assert.equal(backendScopeKey({ profile: 'claudetriad', sessionId: 'A' }), 'claudetriad')
})

test('backendScopeKey: explicit session isolation with missing/empty sessionId throws clearly', () => {
  assert.throws(() => backendScopeKey({ profile: 'claudetriad', isolation: 'session' }), /session isolation requires a sessionId/)
  assert.throws(() => backendScopeKey({ profile: 'claudetriad', sessionId: '', isolation: 'session' }), /sessionId/)
  assert.throws(() => backendScopeKey({ profile: 'claudetriad', sessionId: '   ', isolation: 'session' }), /sessionId/)
})

test('resolveBackendTarget: auto only becomes a session backend under hybrid/session isolation', () => {
  const opts = { isolation: 'auto', sessionId: 's1' }
  // Default 'profile' isolation → auto stays a PROFILE target (a bare string).
  assert.equal(resolveBackendTarget('wikireader', opts, 'profile'), 'wikireader')
  // hybrid / session → session descriptor.
  assert.deepEqual(resolveBackendTarget('wikireader', opts, 'hybrid'), {
    profile: 'wikireader',
    sessionId: 's1',
    isolation: 'session'
  })
  assert.deepEqual(resolveBackendTarget('wikireader', opts, 'session'), {
    profile: 'wikireader',
    sessionId: 's1',
    isolation: 'session'
  })
  // auto without a sessionId is always the profile target.
  assert.equal(resolveBackendTarget('wikireader', { isolation: 'auto' }, 'hybrid'), 'wikireader')
  // no opts / profile isolation → profile target.
  assert.equal(resolveBackendTarget('wikireader', undefined, 'hybrid'), 'wikireader')
  assert.equal(resolveBackendTarget('wikireader', { isolation: 'profile' }, 'hybrid'), 'wikireader')
})

test('resolveBackendTarget: explicit session isolation requires a sessionId', () => {
  assert.deepEqual(resolveBackendTarget('wikireader', { isolation: 'session', sessionId: 's1' }, 'profile'), {
    profile: 'wikireader',
    sessionId: 's1',
    isolation: 'session'
  })
  assert.throws(() => resolveBackendTarget('wikireader', { isolation: 'session' }, 'profile'), /sessionId/)
})

test('backendScopeForTarget maps a resolved target to structured scope (pool-aligned)', () => {
  // Session descriptor → session scope with profile + sessionId.
  assert.deepEqual(backendScopeForTarget({ profile: 'wikireader', sessionId: 's1', isolation: 'session' }, 'default'), {
    scope: 'session',
    profile: 'wikireader',
    sessionId: 's1'
  })
  // Primary-profile string → primary scope; other profile string → profile scope.
  assert.deepEqual(backendScopeForTarget('default', 'default'), { scope: 'primary', profile: 'default' })
  assert.deepEqual(backendScopeForTarget(null, 'default'), { scope: 'primary', profile: null })
  assert.deepEqual(backendScopeForTarget('claudetriad', 'default'), { scope: 'profile', profile: 'claudetriad' })
})

test('normalizeManagementScope: structured scopes → pool targets (Task 10), no raw-key parsing', () => {
  // primary → no pool target.
  assert.deepEqual(normalizeManagementScope({ scope: 'primary', profile: null }, 'default'), {
    scope: 'primary',
    profile: 'default',
    sessionId: null,
    target: null
  })
  // profile naming a NON-primary profile → bare profile-key target.
  assert.deepEqual(normalizeManagementScope({ scope: 'profile', profile: 'claudetriad' }, 'default'), {
    scope: 'profile',
    profile: 'claudetriad',
    sessionId: null,
    target: 'claudetriad'
  })
  // profile naming the PRIMARY profile collapses to primary (no phantom pool op).
  assert.deepEqual(normalizeManagementScope({ scope: 'profile', profile: 'default' }, 'default'), {
    scope: 'primary',
    profile: 'default',
    sessionId: null,
    target: null
  })
  // session → structured session target (never a `session:<p>:<sid>` string).
  assert.deepEqual(normalizeManagementScope({ scope: 'session', profile: 'wikireader', sessionId: 's1' }, 'default'), {
    scope: 'session',
    profile: 'wikireader',
    sessionId: 's1',
    target: { profile: 'wikireader', sessionId: 's1', isolation: 'session' }
  })
})

test('normalizeManagementScope: session without a sessionId, and junk scopes, reject clearly', () => {
  assert.throws(() => normalizeManagementScope({ scope: 'session', profile: 'wikireader' }, 'default'), /sessionId/)
  assert.throws(() => normalizeManagementScope({ scope: 'session', profile: 'wikireader', sessionId: '' }, 'default'), /sessionId/)
  assert.throws(() => normalizeManagementScope({ scope: 'bogus', profile: null }, 'default'), /unknown backend scope/)
  assert.throws(() => normalizeManagementScope(null, 'default'), /structured backend scope/)
  assert.throws(() => normalizeManagementScope('session:wikireader:s1', 'default'), /structured backend scope/)
})

test('describeBackend reports missing vs the live entry lifecycle', async () => {
  const { pool } = sessionPool()
  assert.deepEqual(pool.describeBackend({ profile: 'wikireader', sessionId: 'A', isolation: 'session' }), {
    present: false,
    state: 'missing',
    scope: 'session',
    profile: 'wikireader',
    sessionId: 'A'
  })

  await pool.ensureBackend({ profile: 'wikireader', sessionId: 'A', isolation: 'session' })
  const described = pool.describeBackend({ profile: 'wikireader', sessionId: 'A', isolation: 'session' })
  assert.equal(described.present, true)
  assert.equal(described.scope, 'session')
  assert.equal(described.sessionId, 'A')
  assert.equal(described.state, 'ready')
})

test('CODEX FIX: an auto pop-out under default profile isolation is NOT session-scoped', () => {
  const opts = { isolation: 'auto', sessionId: 'session-1' }
  const primary = 'default'

  // Default 'profile' isolation → resolved target is the profile string, so the
  // scope is 'profile' (a real backend), NOT a phantom session backend.
  const profileTarget = resolveBackendTarget('wikireader', opts, 'profile')
  assert.deepEqual(backendScopeForTarget(profileTarget, primary), { scope: 'profile', profile: 'wikireader' })

  // 'hybrid' isolation → session scope (the session backend genuinely exists).
  const sessionTarget = resolveBackendTarget('wikireader', opts, 'hybrid')
  assert.deepEqual(backendScopeForTarget(sessionTarget, primary), {
    scope: 'session',
    profile: 'wikireader',
    sessionId: 'session-1'
  })
})

function sessionPool(overrides = {}) {
  const spawned = []
  const stopped = []
  const pool = new DesktopBackendPool({
    primaryProfileKey: () => 'default',
    startPrimary: async () => ({ mode: 'primary' }),
    spawnBackend: async (key, entry) => {
      entry.process = fakeProcess(1000 + spawned.length)
      spawned.push({ key, scope: entry.scope, profile: entry.profile, sessionId: entry.sessionId })
      return { key, baseUrl: `http://127.0.0.1/${spawned.length}`, token: 't', process: entry.process }
    },
    healthProbe: overrides.healthProbe || (async () => true),
    stopEntry: (key, entry) => {
      stopped.push(key)
      if (entry?.process) entry.process.kill('SIGTERM')
    },
    waitForBackendExit: overrides.waitForBackendExit || (async () => true),
    tombstoneEntry: overrides.tombstoneEntry || (() => undefined),
    // No-op sleep so the ensureBackend re-probe delay is instant/deterministic in
    // tests; the double-probe code path still runs, just without wall-clock delay.
    sleep: overrides.sleep || (async () => {}),
    setIntervalFn: () => ({ unref() {} }),
    clearIntervalFn: () => {}
  })
  return { pool, spawned, stopped }
}

test('profile backend and a session backend under the same profile are distinct entries', async () => {
  const { pool, spawned } = sessionPool()
  await pool.ensureBackend('claudetriad')
  await pool.ensureBackend({ profile: 'claudetriad', sessionId: 'A', isolation: 'session' })

  assert.equal(pool.size, 2)
  assert.ok(pool.has('claudetriad'))
  assert.ok(pool.has('session:claudetriad:A'))
  assert.equal(pool.get('session:claudetriad:A').scope, 'session')
  assert.equal(pool.get('session:claudetriad:A').sessionId, 'A')
  assert.equal(pool.get('claudetriad').scope, 'profile')
  const sessionSpawn = spawned.find(s => s.key === 'session:claudetriad:A')
  assert.equal(sessionSpawn.profile, 'claudetriad')
  assert.equal(sessionSpawn.sessionId, 'A')
})

test('session A and session B under the same profile are distinct entries', async () => {
  const { pool } = sessionPool()
  await pool.ensureBackend({ profile: 'claudetriad', sessionId: 'A', isolation: 'session' })
  await pool.ensureBackend({ profile: 'claudetriad', sessionId: 'B', isolation: 'session' })
  assert.equal(pool.size, 2)
  assert.notEqual(pool.get('session:claudetriad:A'), pool.get('session:claudetriad:B'))
})

test('tearing down session A does not stop session B or the profile backend', async () => {
  const { pool } = sessionPool()
  await pool.ensureBackend('claudetriad')
  await pool.ensureBackend({ profile: 'claudetriad', sessionId: 'A', isolation: 'session' })
  await pool.ensureBackend({ profile: 'claudetriad', sessionId: 'B', isolation: 'session' })
  const bProc = pool.get('session:claudetriad:B').process
  const profileProc = pool.get('claudetriad').process

  const { confirmed } = await pool.teardownBackendAndWait({ profile: 'claudetriad', sessionId: 'A', isolation: 'session' })

  assert.equal(confirmed, true)
  assert.equal(pool.has('session:claudetriad:A'), false)
  assert.equal(pool.has('session:claudetriad:B'), true)
  assert.equal(pool.has('claudetriad'), true)
  assert.equal(bProc.killed, false, 'session B untouched')
  assert.equal(profileProc.killed, false, 'profile backend untouched')
})

test('health-restart of session A uses confirmed teardown and does not clobber sibling/profile entries', async () => {
  let aHealthy = true
  const { pool, spawned } = sessionPool({
    healthProbe: async (_conn, entry) => {
      if (entry.scope === 'session' && entry.sessionId === 'A') return aHealthy
      return true
    },
    waitForBackendExit: async () => true
  })
  await pool.ensureBackend('claudetriad')
  await pool.ensureBackend({ profile: 'claudetriad', sessionId: 'A', isolation: 'session' })
  await pool.ensureBackend({ profile: 'claudetriad', sessionId: 'B', isolation: 'session' })
  const bProc = pool.get('session:claudetriad:B').process
  const profileProc = pool.get('claudetriad').process

  aHealthy = false
  await pool.ensureBackend({ profile: 'claudetriad', sessionId: 'A', isolation: 'session' })

  assert.equal(spawned.filter(s => s.key === 'session:claudetriad:A').length, 2, 'session A respawned')
  assert.equal(pool.has('session:claudetriad:A'), true)
  assert.equal(bProc.killed, false, 'session B untouched by A restart')
  assert.equal(profileProc.killed, false, 'profile backend untouched by A restart')
})

test('a cached backend that fails one probe then recovers is NOT torn down (re-probe before teardown)', async () => {
  let probeCalls = 0
  const { pool, spawned, stopped } = sessionPool({
    healthProbe: async (_conn, entry) => {
      if (entry.scope === 'session' && entry.sessionId === 'A') {
        probeCalls += 1
        // Fail the FIRST probe, then recover on the re-probe — a transient blip
        // on a backend that is actually still serving.
        return probeCalls !== 1
      }
      return true
    }
  })
  const conn = await pool.ensureBackend({ profile: 'claudetriad', sessionId: 'A', isolation: 'session' })
  const entry = pool.get('session:claudetriad:A')
  const proc = entry.process

  // Re-ensure: first probe fails, re-probe succeeds → NO teardown, same everything.
  const again = await pool.ensureBackend({ profile: 'claudetriad', sessionId: 'A', isolation: 'session' })

  assert.equal(again, conn, 'same connection returned; backend not replaced')
  assert.equal(pool.get('session:claudetriad:A'), entry, 'entry identity unchanged')
  assert.equal(pool.get('session:claudetriad:A').process, proc, 'process not replaced')
  assert.equal(proc.killed, false, 'process never signaled')
  assert.equal(spawned.filter(s => s.key === 'session:claudetriad:A').length, 1, 'no respawn')
  assert.deepEqual(stopped, [], 'no teardown occurred')
  assert.equal(probeCalls, 2, 'probed twice: initial failure + successful re-probe')
})

test('spawn circuit breaker bounds teardown+respawn churn with a clear bounded error', async () => {
  let clock = 0
  const { pool, spawned, stopped } = sessionPool({
    healthProbe: async () => false, // always unhealthy → every ensure tries to respawn
    waitForBackendExit: async () => true // teardown confirmed → respawn would proceed
  })
  pool.now = () => clock // deterministic rolling-window clock

  const target = { profile: 'claudetriad', sessionId: 'A', isolation: 'session' }
  const key = 'session:claudetriad:A'

  // Default respawnBreakerMax = 5: the breaker counts EVERY spawn for the key
  // (the initial one + each respawn). So one initial spawn + four respawns = five,
  // and the sixth spawn attempt trips it.
  await pool.ensureBackend(target) // spawn #1
  for (let i = 0; i < 4; i++) {
    clock += 100
    await pool.ensureBackend(target) // spawns #2..#5 (each: reprobe fail → teardown → respawn)
  }
  assert.equal(spawned.filter(s => s.key === key).length, 5, 'five spawns within the window')

  // The 6th spawn within the window trips the breaker: bounded error, NO new
  // spawn. (The unhealthy entry is still torn down first — a confirmed-dead
  // backend is cleaned up — but no replacement is spawned.)
  clock += 100
  await assert.rejects(() => pool.ensureBackend(target), /spawn budget|churn loop/)
  assert.equal(spawned.filter(s => s.key === key).length, 5, 'breaker did not spawn a 6th')
  assert.equal(pool.has(key), false, 'the over-budget entry was torn down, not left wedged')

  // A slow cadence must NOT stay tripped: advance past the rolling window and the
  // pruned history lets a legitimate spawn proceed again.
  clock += 60_001
  await pool.ensureBackend(target)
  assert.equal(spawned.filter(s => s.key === key).length, 6, 'spawn allowed after window clears')
})

test('a fresh no-entry spawn storm (child-exit/forget/reconnect) is bounded by the breaker', async () => {
  // Reviewer concern (checkpoint 098): a backend that exits cleanly triggers
  // forgetBackend, then an aggressive UI reconnect calls ensureBackend with NO
  // existing entry — the path the old inside-`if (existing)` breaker never saw.
  // With the guard at the spawn chokepoint, this fresh-spawn storm is bounded too.
  let clock = 0
  const { pool, spawned } = sessionPool({ healthProbe: async () => true })
  pool.now = () => clock
  const target = { profile: 'claudetriad', sessionId: 'A', isolation: 'session' }
  const key = 'session:claudetriad:A'

  // Each round: fresh spawn (no entry) → simulate child-exit cleanup via
  // forgetBackend so the NEXT ensure again has no existing entry.
  for (let i = 0; i < 5; i++) {
    clock += 100
    await pool.ensureBackend(target)
    pool.forgetBackend(key)
  }
  assert.equal(spawned.filter(s => s.key === key).length, 5, 'five fresh no-entry spawns recorded')

  clock += 100
  await assert.rejects(() => pool.ensureBackend(target), /spawn budget|churn loop/)
  assert.equal(
    spawned.filter(s => s.key === key).length,
    5,
    'breaker blocked the 6th fresh spawn even with no existing entry'
  )
})

test('session A health timeout respawns only A while sibling and profile descriptors remain unchanged', async () => {
  // Task 12: Desktop-level process-isolation simulation. One session backend's
  // crash/hang (a health-probe TIMEOUT) must respawn ONLY that scope and leave
  // sibling-session and profile descriptors byte-for-byte identical.
  let observedTimeoutMs
  const { pool, spawned, stopped } = sessionPool({
    // A-only failure: the probe THROWS, which isHealthy treats as unhealthy —
    // the same outcome a real health timeout produces. B and the profile stay
    // healthy. Capture the timeout option the pool hands the probe for A.
    healthProbe: async (_conn, entry, opts) => {
      if (entry.scope === 'session' && entry.sessionId === 'A') {
        observedTimeoutMs = opts?.timeoutMs
        throw new Error('health probe timed out (simulated hang)')
      }
      return true
    },
    waitForBackendExit: async () => true
  })

  const profileKey = 'claudetriad'
  const aKey = 'session:claudetriad:A'
  const bKey = 'session:claudetriad:B'

  await pool.ensureBackend(profileKey)
  await pool.ensureBackend({ profile: 'claudetriad', sessionId: 'A', isolation: 'session' })
  await pool.ensureBackend({ profile: 'claudetriad', sessionId: 'B', isolation: 'session' })

  // Snapshot the ORIGINAL entry/connection/process identities for all three.
  const origAEntry = pool.get(aKey)
  const origAConn = origAEntry.connection
  const origAProc = origAEntry.process
  const origBEntry = pool.get(bKey)
  const origBConn = origBEntry.connection
  const origBProc = origBEntry.process
  const origProfileEntry = pool.get(profileKey)
  const origProfileConn = origProfileEntry.connection
  const origProfileProc = origProfileEntry.process

  // Re-ensure A → its cached entry fails the health probe → confirmed teardown →
  // respawn of the SAME key. Only A is probed (only A is re-ensured).
  await pool.ensureBackend({ profile: 'claudetriad', sessionId: 'A', isolation: 'session' })

  // A was respawned: brand-new entry/connection/process, distinct from originals.
  const newAEntry = pool.get(aKey)
  assert.notEqual(newAEntry, origAEntry, 'A entry object was replaced')
  assert.notEqual(newAEntry.connection, origAConn, 'A connection was replaced')
  assert.notEqual(newAEntry.process, origAProc, 'A process was replaced')

  // Only A's original process was signaled/stopped — exactly one SIGTERM, one key.
  assert.equal(origAProc.killed, true, 'original A process was stopped')
  assert.deepEqual(origAProc.signals, ['SIGTERM'], 'original A got a single SIGTERM')
  assert.deepEqual(stopped, [aKey], 'only session A was torn down')

  // Sibling B descriptor is STRICTLY unchanged — same object identities, never killed.
  assert.equal(pool.get(bKey), origBEntry, 'B entry object identity unchanged')
  assert.equal(origBEntry.connection, origBConn, 'B connection identity unchanged')
  assert.equal(origBEntry.process, origBProc, 'B process identity unchanged')
  assert.equal(origBProc.killed, false, 'B process was never signaled')

  // Profile descriptor is STRICTLY unchanged.
  assert.equal(pool.get(profileKey), origProfileEntry, 'profile entry object identity unchanged')
  assert.equal(origProfileEntry.connection, origProfileConn, 'profile connection identity unchanged')
  assert.equal(origProfileEntry.process, origProfileProc, 'profile process identity unchanged')
  assert.equal(origProfileProc.killed, false, 'profile process was never signaled')

  // Pool still holds exactly the three entries: profile, session A, session B.
  assert.equal(pool.size, 3)
  assert.deepEqual(new Set([...pool.keys()]), new Set([profileKey, aKey, bKey]))

  // Spawn accounting: A spawned twice (initial + respawn); B and profile once each.
  assert.equal(spawned.filter(s => s.key === aKey).length, 2, 'A spawned exactly twice')
  assert.equal(spawned.filter(s => s.key === bKey).length, 1, 'B spawned exactly once (no respawn)')
  assert.equal(spawned.filter(s => s.key === profileKey).length, 1, 'profile spawned exactly once (no respawn)')

  // The health-timeout option the pool passed for A equals the configured setting.
  assert.equal(observedTimeoutMs, pool.settings.healthTimeoutMs)
  assert.equal(observedTimeoutMs, 2_500)
})

test('10 session descriptors under one profile create 10 distinct entries without collision', async () => {
  const { pool } = sessionPool()
  for (let i = 0; i < 10; i++) {
    await pool.ensureBackend({ profile: 'claudetriad', sessionId: `s${i}`, isolation: 'session' })
  }
  assert.equal(pool.size, 10)
  const keys = new Set([...pool.keys()])
  assert.equal(keys.size, 10)
  for (let i = 0; i < 10; i++) {
    assert.ok(keys.has(`session:claudetriad:s${i}`))
  }
})

test('forgetBackend by a session key removes only that session entry (profile + sibling intact)', async () => {
  const { pool } = sessionPool()
  await pool.ensureBackend('claudetriad')
  await pool.ensureBackend({ profile: 'claudetriad', sessionId: 'A', isolation: 'session' })
  await pool.ensureBackend({ profile: 'claudetriad', sessionId: 'B', isolation: 'session' })

  // Mirrors the child exit/error cleanup: forget by the POOL KEY, not the profile.
  const removed = pool.forgetBackend('session:claudetriad:A')

  assert.equal(removed, true)
  assert.equal(pool.has('session:claudetriad:A'), false)
  assert.equal(pool.has('session:claudetriad:B'), true, 'sibling session untouched')
  assert.equal(pool.has('claudetriad'), true, 'profile backend untouched by session cleanup')
})

// ── concurrency: per-key serialization (checkpoint 102 live-storm root cause) ──
// The live storm showed "N distinct owner nonces spawned in the same second":
// concurrent ensureBackend() calls for the SAME key each passed the
// await-health/await-teardown gate and each spawned a backend, orphaning the
// healthy ones. ensureBackend() now serializes per key so a burst collapses to a
// single spawn/respawn.

test('concurrent ensureBackend for a fresh key spawns exactly once (coalesced)', async () => {
  const { pool, spawned } = sessionPool()
  const key = 'session:claudetriad:A'
  const target = { profile: 'claudetriad', sessionId: 'A', isolation: 'session' }

  // Fire five simultaneously (no await between) — the storm shape.
  const results = await Promise.all([
    pool.ensureBackend(target),
    pool.ensureBackend(target),
    pool.ensureBackend(target),
    pool.ensureBackend(target),
    pool.ensureBackend(target)
  ])

  assert.equal(spawned.filter(s => s.key === key).length, 1, 'five concurrent callers → exactly one spawn')
  assert.equal(pool.size, 1, 'no orphaned duplicate entries')
  for (const r of results) assert.equal(r, results[0], 'every caller got the same connection')
})

test('concurrent ensureBackend on a stale cached entry respawns exactly once (no orphan storm)', async () => {
  // Model the live defect: the FIRST generation is stale/disowned (probe fails);
  // the respawn is healthy. Without serialization each concurrent caller would
  // tear down + respawn, orphaning healthy backends. With it: one teardown, one
  // respawn, and the queued callers reuse the healthy replacement.
  let firstConn = null
  const { pool, spawned, stopped } = sessionPool({
    healthProbe: async conn => (firstConn === null ? true : conn !== firstConn),
    waitForBackendExit: async () => true
  })
  const key = 'session:claudetriad:A'
  const target = { profile: 'claudetriad', sessionId: 'A', isolation: 'session' }

  await pool.ensureBackend(target) // spawn the (soon-to-be-stale) first generation
  firstConn = pool.get(key).connection // mark it as the disowned/stale one

  const results = await Promise.all([
    pool.ensureBackend(target),
    pool.ensureBackend(target),
    pool.ensureBackend(target),
    pool.ensureBackend(target),
    pool.ensureBackend(target)
  ])

  assert.equal(stopped.filter(k => k === key).length, 1, 'exactly one teardown, not five')
  assert.equal(spawned.filter(s => s.key === key).length, 2, 'initial + exactly one respawn')
  assert.equal(pool.size, 1, 'no orphaned extra entries left behind')
  const healthy = pool.get(key).connection
  assert.notEqual(healthy, firstConn, 'the surviving entry is the healthy replacement')
  for (const r of results) assert.equal(r, healthy, 'every queued caller reused the one healthy respawn')
})

test('concurrent ensureBackend on a healthy cached entry never respawns', async () => {
  const { pool, spawned, stopped } = sessionPool() // default healthy probe
  const key = 'session:claudetriad:A'
  const target = { profile: 'claudetriad', sessionId: 'A', isolation: 'session' }

  const first = await pool.ensureBackend(target)
  const results = await Promise.all(Array.from({ length: 6 }, () => pool.ensureBackend(target)))

  for (const r of results) assert.equal(r, first, 'healthy reuse returns the same connection to all callers')
  assert.equal(spawned.filter(s => s.key === key).length, 1, 'no respawn for a healthy backend under concurrency')
  assert.deepEqual(stopped, [], 'no teardown for a healthy backend')
  assert.equal(pool.size, 1)
})
