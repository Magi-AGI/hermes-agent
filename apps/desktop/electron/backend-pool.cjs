'use strict'

const fs = require('node:fs')

const DEFAULT_BACKEND_POOL_SETTINGS = Object.freeze({
  maxProfileBackends: 3,
  maxSessionBackendsPerProfile: 10,
  spawnConcurrency: 10,
  idleMs: 10 * 60_000,
  keepaliveFreshMs: 90_000,
  healthTimeoutMs: 2_500,
  startupTimeoutMs: 90_000,
  autoReapOrphans: true,
  // Re-probe gap: a cached backend that fails ONE health probe is re-probed once
  // after this delay before any destructive teardown. A backend that is actually
  // still serving (a transient GC pause / momentary reset) survives instead of
  // triggering a churny teardown+respawn. 0 disables the re-probe.
  healthRecheckDelayMs: 300,
  // Respawn circuit breaker: at most this many teardown+respawn cycles per key
  // within the rolling window. Beyond it, ensureBackend surfaces a BOUNDED error
  // instead of looping forever (defense-in-depth against a disown/churn storm).
  respawnBreakerMax: 5,
  respawnBreakerWindowMs: 60_000,
  // profile | hybrid | session. profile = current behavior (one backend per
  // profile). hybrid = pop-outs get session backends. session = every isolated
  // session gets its own backend. Anything else falls back to profile.
  backendIsolation: 'profile'
})

const BACKEND_ISOLATION_MODES = ['profile', 'hybrid', 'session']

function isolationSetting(value, fallback) {
  const normalized = String(value == null ? '' : value).trim().toLowerCase()
  return BACKEND_ISOLATION_MODES.includes(normalized) ? normalized : fallback
}

function numberSetting(value, fallback, { min = 0 } = {}) {
  const parsed = Number(value)
  if (!Number.isFinite(parsed)) return fallback
  return Math.max(min, Math.round(parsed))
}

function boolSetting(value, fallback) {
  if (typeof value === 'boolean') return value
  if (typeof value === 'string') {
    const normalized = value.trim().toLowerCase()
    if (['1', 'true', 'yes', 'on'].includes(normalized)) return true
    if (['0', 'false', 'no', 'off'].includes(normalized)) return false
  }
  return fallback
}

function resolveBackendPoolSettings(config = {}) {
  const desktop = config?.desktop || {}
  const raw = desktop.backend_pool || {}
  // Canonical isolation lives at top-level desktop.backend_isolation; the legacy
  // nested desktop.backend_pool.backend_isolation is still accepted. An
  // explicitly-set top-level value (non-null, non-blank) wins over the nested
  // alias. Invalid values fall back to profile via isolationSetting.
  const topIsolation = desktop.backend_isolation
  const topPresent = topIsolation != null && String(topIsolation).trim() !== ''
  const isolationRaw = topPresent ? topIsolation : raw.backend_isolation
  return {
    maxProfileBackends: numberSetting(raw.max_profile_backends, DEFAULT_BACKEND_POOL_SETTINGS.maxProfileBackends, { min: 1 }),
    maxSessionBackendsPerProfile: numberSetting(
      raw.max_session_backends_per_profile,
      DEFAULT_BACKEND_POOL_SETTINGS.maxSessionBackendsPerProfile,
      { min: 1 }
    ),
    spawnConcurrency: numberSetting(raw.spawn_concurrency, DEFAULT_BACKEND_POOL_SETTINGS.spawnConcurrency, { min: 1 }),
    idleMs: numberSetting(raw.idle_ms, DEFAULT_BACKEND_POOL_SETTINGS.idleMs, { min: 60_000 }),
    keepaliveFreshMs: numberSetting(raw.keepalive_fresh_ms, DEFAULT_BACKEND_POOL_SETTINGS.keepaliveFreshMs, { min: 1_000 }),
    healthTimeoutMs: numberSetting(raw.health_timeout_ms, DEFAULT_BACKEND_POOL_SETTINGS.healthTimeoutMs, { min: 250 }),
    startupTimeoutMs: numberSetting(raw.startup_timeout_ms, DEFAULT_BACKEND_POOL_SETTINGS.startupTimeoutMs, { min: 45_000 }),
    healthRecheckDelayMs: numberSetting(raw.health_recheck_delay_ms, DEFAULT_BACKEND_POOL_SETTINGS.healthRecheckDelayMs, { min: 0 }),
    respawnBreakerMax: numberSetting(raw.respawn_breaker_max, DEFAULT_BACKEND_POOL_SETTINGS.respawnBreakerMax, { min: 1 }),
    respawnBreakerWindowMs: numberSetting(raw.respawn_breaker_window_ms, DEFAULT_BACKEND_POOL_SETTINGS.respawnBreakerWindowMs, { min: 1_000 }),
    autoReapOrphans: boolSetting(raw.auto_reap_orphans, DEFAULT_BACKEND_POOL_SETTINGS.autoReapOrphans),
    backendIsolation: isolationSetting(isolationRaw, DEFAULT_BACKEND_POOL_SETTINGS.backendIsolation)
  }
}

function parseYamlScalar(value) {
  const trimmed = String(value || '').trim()
  if (!trimmed) return ''
  if (/^(true|false)$/i.test(trimmed)) return trimmed.toLowerCase() === 'true'
  if (/^-?\d+(?:\.\d+)?$/.test(trimmed)) return Number(trimmed)
  return trimmed.replace(/^['"]|['"]$/g, '')
}

function readBackendPoolSettingsFromConfig(configPath) {
  let text
  try {
    text = fs.readFileSync(configPath, 'utf8')
  } catch {
    return resolveBackendPoolSettings({})
  }

  const backendPool = {}
  // Direct children of `desktop:` (e.g. the canonical top-level
  // `backend_isolation:` scalar), kept separate from backend_pool scalars.
  const desktopScalars = {}
  const lines = text.replace(/^\uFEFF/, '').split(/\r?\n/)
  let inDesktop = false
  let desktopIndent = null
  let inBackendPool = false
  let backendPoolIndent = null
  for (const rawLine of lines) {
    const withoutComment = rawLine.replace(/\s+#.*$/, '')
    if (!withoutComment.trim()) continue
    const indent = withoutComment.match(/^\s*/)[0].length
    const trimmed = withoutComment.trim()
    if (trimmed === 'desktop:') {
      inDesktop = true
      desktopIndent = indent
      inBackendPool = false
      backendPoolIndent = null
      continue
    }
    if (inDesktop && indent <= desktopIndent && trimmed.endsWith(':')) {
      inDesktop = false
      inBackendPool = false
    }
    if (inDesktop && trimmed === 'backend_pool:') {
      inBackendPool = true
      backendPoolIndent = indent
      continue
    }
    if (inBackendPool && indent <= backendPoolIndent) {
      inBackendPool = false
    }
    const match = trimmed.match(/^([A-Za-z0-9_]+):\s*(.*?)\s*$/)
    if (!match) continue
    if (inBackendPool) {
      backendPool[match[1]] = parseYamlScalar(match[2])
    } else if (inDesktop && indent > desktopIndent && match[2] !== '') {
      // A scalar directly under desktop: (has an inline value), e.g.
      // `backend_isolation: hybrid`. Blank values (`key:` opening a nested
      // block) are skipped so we don't record section headers as scalars.
      desktopScalars[match[1]] = parseYamlScalar(match[2])
    }
  }
  return resolveBackendPoolSettings({ desktop: { ...desktopScalars, backend_pool: backendPool } })
}

function normalizeProfileKey(profile, fallback = null) {
  const key = profile && String(profile).trim() ? String(profile).trim() : fallback
  return key || null
}

// Pure pool identity key for a backend descriptor (M4a). Profile scope keeps the
// bare profile string (backwards compatible with the profile-only pool); session
// scope is `session:<profile>:<sessionId>` so a profile backend and each of its
// sibling session backends are DISTINCT entries. Session isolation without a
// sessionId is a clear error — never a silent fallback that would alias sessions.
function backendScopeKey({ profile, sessionId, isolation } = {}) {
  const profileKey = normalizeProfileKey(profile) || 'default'
  if (isolation === 'session') {
    const sid = sessionId != null ? String(sessionId).trim() : ''
    if (!sid) throw new Error('session isolation requires a sessionId')
    return `session:${profileKey}:${sid}`
  }
  return profileKey
}

// Resolve a (profile, opts) request into a pool target: a profile string (→
// profile/primary backend) or a session descriptor. The URL's sessionId is
// renderer INTENT; whether it becomes a SESSION backend depends on the
// configured backendIsolation (auto → session ONLY in hybrid/session mode). Pure
// so the renderer status model can align to the SAME resolution main performs.
function resolveBackendTarget(profile, opts, backendIsolation) {
  const isolation = opts && opts.isolation
  const sessionId = opts && opts.sessionId != null ? String(opts.sessionId).trim() : ''
  if (isolation === 'session') {
    if (!sessionId) throw new Error('session isolation requires a sessionId')
    return { profile, sessionId, isolation: 'session' }
  }
  if (isolation === 'auto') {
    if ((backendIsolation === 'hybrid' || backendIsolation === 'session') && sessionId) {
      return { profile, sessionId, isolation: 'session' }
    }
    return profile
  }
  return profile
}

// Structured scope of a RESOLVED target (from resolveBackendTarget), mirroring
// the pool identity so renderer status and main backends line up 1:1:
//   session descriptor         → { scope: 'session', profile, sessionId }
//   profile string === primary → { scope: 'primary', profile }
//   any other profile string   → { scope: 'profile', profile }
function backendScopeForTarget(target, primaryProfileKey) {
  const primary = normalizeProfileKey(primaryProfileKey)
  if (target && typeof target === 'object' && target.isolation === 'session') {
    return { scope: 'session', profile: normalizeProfileKey(target.profile) || primary, sessionId: String(target.sessionId) }
  }
  const profileKey = normalizeProfileKey(target)
  if (!profileKey || profileKey === primary) {
    return { scope: 'primary', profile: profileKey || null }
  }
  return { scope: 'profile', profile: profileKey }
}

// Validate a STRUCTURED management scope (never a raw status key) and derive the
// pool target for it (Task 10). Returns { scope, profile, sessionId, target }:
//   - primary → target null (handled by the primary backend, not the pool)
//   - profile naming the primary profile (or none) → collapses to primary
//   - profile → target is the bare profile key (pool profile backend)
//   - session → target is { profile, sessionId, isolation: 'session' }; a missing
//     sessionId is a CLEAR error, never collapsed onto the profile.
function normalizeManagementScope(scope, primaryProfileKey) {
  if (!scope || typeof scope !== 'object' || typeof scope.scope !== 'string') {
    throw new Error('a structured backend scope { scope, profile, sessionId? } is required')
  }
  const primary = normalizeProfileKey(primaryProfileKey)
  const profileKey = normalizeProfileKey(scope.profile)

  if (scope.scope === 'primary') {
    return { scope: 'primary', profile: profileKey || primary || null, sessionId: null, target: null }
  }

  if (scope.scope === 'profile') {
    // A profile scope naming the primary profile (or none) IS the primary backend.
    if (!profileKey || profileKey === primary) {
      return { scope: 'primary', profile: primary || null, sessionId: null, target: null }
    }
    return { scope: 'profile', profile: profileKey, sessionId: null, target: profileKey }
  }

  if (scope.scope === 'session') {
    const sid = scope.sessionId != null ? String(scope.sessionId).trim() : ''
    if (!sid) throw new Error('session scope requires a sessionId')
    const profile = profileKey || primary || 'default'
    return { scope: 'session', profile, sessionId: sid, target: { profile, sessionId: sid, isolation: 'session' } }
  }

  throw new Error(`unknown backend scope: ${scope.scope}`)
}

class DesktopBackendPool {
  constructor(options = {}) {
    this.settings = { ...DEFAULT_BACKEND_POOL_SETTINGS, ...resolveBackendPoolSettings(options.config), ...(options.settings || {}) }
    this.primaryProfileKey = options.primaryProfileKey || (() => 'default')
    this.startPrimary = options.startPrimary || (async () => {
      throw new Error('startPrimary is required')
    })
    this.spawnBackend = options.spawnBackend || (async () => {
      throw new Error('spawnBackend is required')
    })
    this.healthProbe = options.healthProbe || (async () => true)
    this.stopEntry = options.stopEntry || ((_profile, entry) => {
      if (entry?.process && !entry.process.killed) {
        try {
          entry.process.kill('SIGTERM')
        } catch {
          // Already gone.
        }
      }
    })
    this.forgetEntry = options.forgetEntry || (() => undefined)
    // Called when a teardown could NOT confirm the process exited: the caller
    // keeps the ownership record but tombstones its lifecycle so the reaper can
    // still find the survivor. Removal of confirmed-exited backends happens on
    // the child 'exit'/'error' events, not here.
    this.tombstoneEntry = options.tombstoneEntry || (() => undefined)
    this.waitForBackendExit = options.waitForBackendExit || (async () => true)
    this.now = options.now || (() => Date.now())
    this.sleep = options.sleep || (ms => new Promise(resolve => setTimeout(resolve, ms)))
    this.log = options.log || (() => undefined)
    this.setIntervalFn = options.setIntervalFn || setInterval
    this.clearIntervalFn = options.clearIntervalFn || clearInterval
    this.entries = new Map()
    this.idleReaper = null
    // Per-key teardown+respawn timestamps for the circuit breaker.
    this.respawnHistory = new Map()
    // Per-key in-flight operation tail, so concurrent ensureBackend() calls for
    // the same key serialize instead of each spawning a racing backend.
    this.inFlight = new Map()
  }

  // Normalize a target (legacy profile string/null, or a {profile, sessionId,
  // isolation} descriptor) into { scope, profile, sessionId, key }. The primary
  // profile's fallback is the pool's primaryProfileKey (not 'default'). A bare
  // key string (e.g. a session key from an internal loop) passes through as a
  // profile-scope "key" unchanged, which is what the entry map is keyed by.
  _normalizeTarget(target) {
    const primary = this.primaryProfileKey()
    if (target && typeof target === 'object' && target.isolation === 'session') {
      const profileKey = normalizeProfileKey(target.profile, primary)
      const sid = target.sessionId != null ? String(target.sessionId).trim() : ''
      if (!sid) throw new Error('session isolation requires a sessionId')
      return { scope: 'session', profile: profileKey, sessionId: sid, key: `session:${profileKey}:${sid}` }
    }
    const profileArg = target && typeof target === 'object' ? target.profile : target
    const profileKey = normalizeProfileKey(profileArg, primary)
    return { scope: 'profile', profile: profileKey, sessionId: null, key: profileKey }
  }

  keyFor(target) {
    return this._normalizeTarget(target).key
  }

  // Status of a pool entry for a target (Task 10 backendStatus). 'missing' when
  // no entry exists; otherwise the entry's lifecycle (starting/ready/stopping/
  // failed). Scope/profile/sessionId are echoed for the caller's status object.
  describeBackend(target) {
    const { scope, profile, sessionId, key } = this._normalizeTarget(target)
    const entry = this.entries.get(key)
    return {
      present: Boolean(entry),
      state: entry ? entry.lifecycle || 'starting' : 'missing',
      scope,
      profile,
      sessionId
    }
  }

  get size() {
    return this.entries.size
  }

  get(profile) {
    return this.entries.get(profile)
  }

  has(profile) {
    return this.entries.has(profile)
  }

  values() {
    return this.entries.values()
  }

  keys() {
    return this.entries.keys()
  }

  async ensureBackend(target) {
    const descriptor = this._normalizeTarget(target)
    const primary = this.primaryProfileKey()
    // Only the PROFILE-scope primary key routes to the primary backend; a session
    // descriptor for the primary profile gets its own pooled entry (its key has
    // the `session:` prefix, so it can never alias the primary backend).
    if (descriptor.scope === 'profile' && descriptor.key === primary) return this.startPrimary()

    const key = descriptor.key
    // Per-key SERIALIZATION. Concurrent callers for the same key (a profile switch
    // that fires the gateway connect plus several requests at once) must NOT each
    // run the check-health → teardown → spawn sequence: the `await` points let
    // every caller pass the "should I spawn?" gate simultaneously, so each spawned
    // a fresh backend and the last `entries.set` clobbered the rest — orphaning
    // healthy backends and producing the observed "N distinct owner nonces spawned
    // in the same second" storm that never converges. Chaining every operation for
    // a key onto the previous one collapses that burst into ONE spawn; the queued
    // callers then simply reuse the ready connection.
    const prior = this.inFlight.get(key) || Promise.resolve()
    const run = (async () => {
      try {
        await prior
      } catch {
        // A prior op's rejection belongs to ITS caller; we still run ours.
      }
      return this._ensureBackendLocked(descriptor, key)
    })()
    // New tail marker (settle-only). Clear it on completion only if it is still the
    // current tail, so a newer queued op is never dropped.
    const marker = run.then(
      () => {},
      () => {}
    )
    this.inFlight.set(key, marker)
    marker.then(() => {
      if (this.inFlight.get(key) === marker) this.inFlight.delete(key)
    })
    return run
  }

  async _ensureBackendLocked(descriptor, key) {
    const existing = this.entries.get(key)
    if (existing) {
      existing.lastActiveAt = this.now()
      if (!existing.connection) return existing.connectionPromise
      if (await this.isHealthy(existing.connection, existing)) {
        return existing.connection
      }
      // Re-probe once before any destructive teardown. A backend that is actually
      // still serving (a transient GC pause, a momentary connection reset, a
      // brief token-adoption gap after a disown) must NOT be torn down on a single
      // failed probe — that single-shot verdict is what turned a healthy backend
      // into a teardown+respawn churn loop. Only a backend that fails TWICE (with
      // a short gap) is treated as unhealthy.
      if (this.settings.healthRecheckDelayMs > 0) {
        await this.sleep(this.settings.healthRecheckDelayMs)
        if (await this.isHealthy(existing.connection, existing)) {
          return existing.connection
        }
      }
      // R10: confirm the old generation is really gone BEFORE spawning a same-id
      // replacement. A signal-only stop that leaves the old process hung would
      // let the new same-id upsert clobber the old row, orphaning a live
      // process. If teardown can't be confirmed the old generation is tombstoned
      // + retained for the reaper, and we REFUSE to respawn (fail clearly) rather
      // than clobber it.
      this.log(`Cached profile backend "${key}" failed health probe; tearing down before restart.`)
      const { confirmed } = await this.teardownBackendAndWait(key)
      if (!confirmed) {
        throw new Error(
          `Profile backend "${key}" is unresponsive and could not be confirmed stopped; ` +
            'not spawning a same-id replacement. Retry shortly — the reaper will clear the wedged backend.'
        )
      }
    }

    // Circuit breaker at the SINGLE spawn chokepoint: every actual spawn for this
    // key — a health-restart respawn AND a fresh no-entry spawn — is rate-checked
    // here. This bounds not only teardown+respawn churn but also a child-exit →
    // forgetBackend → aggressive-reconnect storm, where the entry is already gone
    // before ensureBackend() runs (so the old inside-`if (existing)` placement
    // never saw it). Healthy cached reuse returns earlier and never reaches here,
    // so it does not consume the budget. Over budget → a BOUNDED error, no spawn.
    this.assertRespawnWithinBudget(key)

    this.evictLru(this.settings.maxProfileBackends - 1)

    const entry = {
      scope: descriptor.scope,
      profile: descriptor.profile,
      sessionId: descriptor.sessionId,
      process: null,
      port: null,
      token: null,
      connection: null,
      connectionPromise: null,
      lastActiveAt: this.now(),
      lifecycle: 'starting'
    }
    entry.connectionPromise = this.spawnBackend(key, entry)
      .then(connection => {
        entry.connection = connection
        entry.lifecycle = 'ready'
        return connection
      })
      .catch(error => {
        if (entry.process || entry.registryId || entry.sidecarPath) {
          this.stopEntry(key, entry)
        }
        this.entries.delete(key)
        entry.lifecycle = 'failed'
        throw error
      })
    this.entries.set(key, entry)
    this.startIdleReaper()
    return entry.connectionPromise
  }

  async isHealthy(connection, entry) {
    try {
      return (await this.healthProbe(connection, entry, { timeoutMs: this.settings.healthTimeoutMs })) !== false
    } catch {
      return false
    }
  }

  // Circuit breaker for spawn churn on a single key. Called once per ACTUAL spawn
  // (health-restart respawn OR fresh no-entry spawn), so it bounds both a
  // teardown+respawn loop and a child-exit/forget/reconnect storm. Records this
  // spawn for `key` and throws a bounded error if `respawnBreakerMax` spawns have
  // already occurred within `respawnBreakerWindowMs`. Old timestamps outside the
  // window are pruned so a slow, healthy spawn cadence never trips it. Healthy
  // cached reuse returns before this point and never consumes the budget.
  assertRespawnWithinBudget(key) {
    const now = this.now()
    const windowMs = this.settings.respawnBreakerWindowMs
    const max = this.settings.respawnBreakerMax
    const recent = (this.respawnHistory.get(key) || []).filter(ts => now - ts < windowMs)
    if (recent.length >= max) {
      this.respawnHistory.set(key, recent)
      throw new Error(
        `Backend "${key}" exceeded the spawn budget (${max} spawns in ` +
          `${Math.round(windowMs / 1000)}s); refusing to spawn to avoid a churn loop. ` +
          'Retry shortly — the reaper will clear any wedged generation.'
      )
    }
    recent.push(now)
    this.respawnHistory.set(key, recent)
  }

  touchBackend(target) {
    let key
    try {
      key = this._normalizeTarget(target).key
    } catch {
      return // e.g. session isolation without a sessionId — nothing to touch
    }
    if (!key) return
    const entry = this.entries.get(key)
    if (entry) entry.lastActiveAt = this.now()
  }

  evictLru(keep) {
    if (this.entries.size <= keep) return
    const now = this.now()
    const evictable = [...this.entries.entries()]
      .filter(([, entry]) => now - (entry.lastActiveAt || 0) > this.settings.keepaliveFreshMs)
      .sort((a, b) => (a[1].lastActiveAt || 0) - (b[1].lastActiveAt || 0))
    let removable = this.entries.size - Math.max(0, keep)
    for (const [profile] of evictable) {
      if (removable <= 0) break
      this.log(`Evicting idle profile backend "${profile}" (LRU cap ${this.settings.maxProfileBackends})`)
      // Confirmed teardown (not signal-only): the entry is removed from the map
      // synchronously, but if the process hangs past force-kill it is tombstoned
      // + retained so a later same-id respawn can't silently clobber a survivor.
      this.confirmTeardownInBackground(profile)
      removable -= 1
    }
  }

  // Fire a confirmed teardown without blocking the caller. Used by best-effort
  // cleanup paths (LRU eviction, idle reap) where we must not leave a hung old
  // generation as a stale live row. Deletion from the map happens synchronously
  // (teardownBackendAndWait runs to its first await before returning the
  // promise), so pool-size accounting stays correct.
  confirmTeardownInBackground(profile) {
    Promise.resolve(this.teardownBackendAndWait(profile)).catch(() => undefined)
  }

  startIdleReaper() {
    if (this.idleReaper) return
    this.idleReaper = this.setIntervalFn(() => {
      this.reapIdle()
    }, 60_000)
    if (typeof this.idleReaper?.unref === 'function') this.idleReaper.unref()
  }

  reapIdle() {
    const now = this.now()
    for (const [profile, entry] of [...this.entries.entries()]) {
      if (now - (entry.lastActiveAt || 0) > this.settings.idleMs) {
        this.log(`Reaping idle profile backend "${profile}" (idle > ${Math.round(this.settings.idleMs / 1000)}s)`)
        this.confirmTeardownInBackground(profile)
      }
    }
    if (this.entries.size === 0) this.stopIdleReaper()
  }

  stopIdleReaper() {
    if (!this.idleReaper) return
    this.clearIntervalFn(this.idleReaper)
    this.idleReaper = null
  }

  stopBackend(profile) {
    const entry = this.entries.get(profile)
    if (!entry) return
    this.entries.delete(profile)
    entry.lifecycle = 'stopping'
    this.stopEntry(profile, entry)
    if (this.entries.size === 0) this.stopIdleReaper()
  }

  forgetBackend(profile) {
    const entry = this.entries.get(profile)
    const existed = this.entries.delete(profile)
    if (existed) this.forgetEntry(profile, entry)
    if (this.entries.size === 0) this.stopIdleReaper()
    return existed
  }

  async teardownBackendAndWait(target) {
    // Accepts a profile string, a session key string, or a {profile, sessionId,
    // isolation} descriptor — so session backends tear down through the SAME
    // confirmed path as profile backends (no signal-only stop for session scope).
    const key = this._normalizeTarget(target).key
    const entry = this.entries.get(key)
    if (!entry) return { confirmed: true } // nothing to tear down
    this.entries.delete(key)
    entry.lifecycle = 'stopping'
    this.stopEntry(key, entry)
    // Only finalize (drop the registry row + sidecar) once exit is CONFIRMED;
    // that removal is driven by the child 'exit' handler. If exit can't be
    // confirmed (hung past the force-kill), tombstone so the reaper keeps the
    // row and can retry — never orphan a survivor by deleting its record early.
    const exited = await this.waitForBackendExit(entry.process)
    if (!exited) {
      entry.lifecycle = 'failed'
      this.tombstoneEntry(key, entry)
    }
    if (this.entries.size === 0) this.stopIdleReaper()
    return { confirmed: Boolean(exited) }
  }

  stopAllBackends() {
    for (const profile of [...this.entries.keys()]) {
      this.stopBackend(profile)
    }
  }

  stopAll() {
    this.stopAllBackends()
  }

  childPids() {
    const pids = []
    for (const entry of this.entries.values()) {
      if (entry.process && Number.isInteger(entry.process.pid)) pids.push(entry.process.pid)
    }
    return pids
  }
}

module.exports = {
  DEFAULT_BACKEND_POOL_SETTINGS,
  DesktopBackendPool,
  backendScopeForTarget,
  backendScopeKey,
  normalizeManagementScope,
  normalizeProfileKey,
  readBackendPoolSettingsFromConfig,
  resolveBackendPoolSettings,
  resolveBackendTarget
}
