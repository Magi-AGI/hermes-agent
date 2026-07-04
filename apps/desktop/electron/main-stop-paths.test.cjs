const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')

// Static regression guards on main.cjs's backend stop paths (R12). These lock in
// the invariant that any path capable of a same-profile REOPEN uses confirmed
// teardown (tombstone-on-unconfirmed), so a hung backend can never be left as a
// clobberable 'ready' row — and that signal-only stop is reserved, by name, for
// shutdown paths only. M4 session-backend controls must uphold the same rule.
const mainSrc = fs.readFileSync(path.join(__dirname, 'main.cjs'), 'utf8')

test('main.cjs never calls signal-only single-profile stopBackend (reopen-clobber risk)', () => {
  assert.equal(
    mainSrc.includes('profileBackendPool.stopBackend('),
    false,
    'single-profile stops must route through confirmed teardown, not signal-only stopBackend'
  )
})

test('reopen-capable stopPoolBackend routes through confirmed teardown', () => {
  const match = mainSrc.match(/async function stopPoolBackend\(profile\) \{([\s\S]*?)\n\}/)
  assert.ok(match, 'stopPoolBackend should be an async function')
  assert.ok(
    /teardownBackendAndWait/.test(match[1]),
    'stopPoolBackend must delegate to teardownBackendAndWait (confirmed teardown)'
  )
})

test('the connection-config apply path awaits the confirmed stop', () => {
  assert.ok(mainSrc.includes('await stopPoolBackend(key)'))
})

test('signal-only shutdown helper is named for its restricted purpose and the ambiguous name is gone', () => {
  assert.ok(mainSrc.includes('function signalAllPoolBackendsForShutdown()'))
  assert.equal(
    /\bstopAllPoolBackends\b/.test(mainSrc),
    false,
    'the ambiguous stopAllPoolBackends name must be gone so no reopen path can call a signal-only stop'
  )
})

// ── Task 10: backend management IPC uses confirmed teardown (static guards) ──

test('managed restart/stop route through confirmed teardown, never signal-only stopBackend', () => {
  const restart = mainSrc.match(/async function restartManagedBackend\(scope\) \{([\s\S]*?)\n\}/)
  const stop = mainSrc.match(/async function stopManagedBackend\(scope\) \{([\s\S]*?)\n\}/)
  assert.ok(restart, 'restartManagedBackend should exist')
  assert.ok(stop, 'stopManagedBackend should exist')
  // Both must confirm teardown for pool scopes and the primary; neither may use
  // the signal-only single-profile stopBackend.
  assert.ok(/teardownBackendAndWait/.test(restart[1]), 'restart must use teardownBackendAndWait for pool scopes')
  assert.ok(/teardownPrimaryBackendAndWait/.test(restart[1]), 'restart must confirm primary teardown')
  assert.ok(/teardownBackendAndWait/.test(stop[1]), 'stop must use teardownBackendAndWait for pool scopes')
  assert.ok(/teardownPrimaryBackendAndWait/.test(stop[1]), 'stop must confirm primary teardown')
  assert.equal(/profileBackendPool\.stopBackend\(/.test(restart[1]), false)
  assert.equal(/profileBackendPool\.stopBackend\(/.test(stop[1]), false)
})

test('management IPC handlers are registered and operate on structured scopes', () => {
  for (const channel of ['hermes:backend:status', 'hermes:backend:restart', 'hermes:backend:stop', 'hermes:backend:reconcile']) {
    assert.ok(mainSrc.includes(`ipcMain.handle('${channel}'`), `${channel} handler must be registered`)
  }
  // Scope validation is delegated to the pure normalizeManagementScope (no raw
  // status-key parsing like `.split(':')` in the management path).
  assert.ok(mainSrc.includes('normalizeManagementScope(scope, primaryProfileKey())'))
})

test('primary restart fails closed on unconfirmed teardown — never starts a replacement', () => {
  const restart = mainSrc.match(/async function restartManagedBackend\(scope\) \{([\s\S]*?)\n\}/)[1]
  // Isolate the primary branch: from `norm.scope === 'primary'` up to the pool
  // teardown of exactly-this-scope that begins the non-primary path.
  const primaryBranch = restart.slice(0, restart.indexOf('profileBackendPool.teardownBackendAndWait'))
  assert.ok(
    /const\s+confirmed\s*=\s*await\s+teardownPrimaryBackendAndWait\(\)/.test(primaryBranch),
    'primary restart must capture the confirmed boolean'
  )
  // The unconfirmed guard must return BEFORE startHermes is reached.
  const guardIdx = primaryBranch.search(/if\s*\(\s*!confirmed\s*\)/)
  const startIdx = primaryBranch.indexOf('startHermes(')
  assert.ok(guardIdx !== -1, 'primary restart must guard on !confirmed')
  assert.ok(startIdx !== -1, 'primary restart must (eventually) call startHermes')
  assert.ok(guardIdx < startIdx, 'the !confirmed guard must precede startHermes')
  assert.ok(/teardown-unconfirmed/.test(primaryBranch), 'unconfirmed primary restart reports teardown-unconfirmed')
  // Prove the guard body returns fail-closed (does not fall through to startHermes).
  const guardBody = primaryBranch.slice(guardIdx, startIdx)
  assert.ok(/ok:\s*false/.test(guardBody), 'unconfirmed primary restart returns ok:false')
  assert.equal(/startHermes\(/.test(guardBody), false, 'no startHermes inside the unconfirmed guard')
})

test('stop fails closed on unconfirmed teardown for BOTH primary and pool scopes', () => {
  const stop = mainSrc.match(/async function stopManagedBackend\(scope\) \{([\s\S]*?)\n\}/)[1]
  // Every teardown result in stop must be gated by a !confirmed fail-closed check.
  const failClosed = stop.match(/if\s*\(\s*!confirmed\s*\)\s*\{\s*return\s*\{\s*ok:\s*false,\s*confirmed:\s*false,\s*error:\s*'teardown-unconfirmed'/g) || []
  assert.ok(failClosed.length >= 2, 'both primary and pool stop paths must fail closed on unconfirmed teardown')
  // A confirmed stop is the only ok:true.
  assert.ok(/ok:\s*true,\s*confirmed:\s*true/.test(stop), 'confirmed stop returns ok:true, confirmed:true')
  // Legacy soft-success shape must be gone.
  assert.equal(/ok:\s*true,\s*confirmed:\s*Boolean\(confirmed\)/.test(stop), false, 'no soft ok:true on unconfirmed stop')
})

test('primary status derives from an explicit lifecycle phase, not the process handle', () => {
  // A truthful tracker exists and is reset/advanced at the boot transitions.
  assert.ok(/let\s+primaryBackendPhase\s*=\s*'missing'/.test(mainSrc), 'primaryBackendPhase tracker must exist')
  assert.ok(/primaryBackendPhase\s*=\s*'starting'/.test(mainSrc), 'boot start sets starting')
  assert.ok(/primaryBackendPhase\s*=\s*'ready'/.test(mainSrc), 'resolved connection sets ready (local AND remote)')
  assert.ok(/primaryBackendPhase\s*=\s*'missing'/.test(mainSrc), 'teardown/exit sets missing')

  const state = mainSrc.match(/function primaryBackendState\(\) \{([\s\S]*?)\n\}/)
  assert.ok(state, 'primaryBackendState() helper must exist')
  // Latched failures win over the phase so a wedged boot never reads ready/starting.
  assert.ok(/bootstrapFailure\s*\|\|\s*backendStartFailure/.test(state[1]), 'latched failure maps to failed')
  assert.ok(/return\s+primaryBackendPhase/.test(state[1]), 'otherwise reports the tracked phase')

  // The old, inaccurate signals must be gone from the primary status branch.
  const status = mainSrc.match(/function managedBackendStatus\(scope\) \{([\s\S]*?)\n\}/)[1]
  assert.ok(/state:\s*primaryBackendState\(\)/.test(status), 'primary status uses primaryBackendState()')
  assert.equal(
    /hermesProcess\s*&&\s*!hermesProcess\.killed/.test(status),
    false,
    'primary status must not equate a live process handle with ready'
  )
  assert.equal(
    /connectionPromise\s*\?\s*'starting'/.test(status),
    false,
    'primary status must not report remote-ready as forever starting'
  )
})

test('unconfirmed primary teardown is latched, blocks startHermes, and re-targets the child', () => {
  // teardownPrimaryBackendAndWait must arm the latch on unconfirmed exit, clear
  // it on confirmed exit, and re-target the LATCHED child when the live handle
  // is already null (repeat-call safety).
  const teardown = mainSrc.match(/async function teardownPrimaryBackendAndWait\(\) \{([\s\S]*?)\n\}/)[1]
  assert.ok(/primaryTeardownLatch\.isActive\(\)/.test(teardown), 'repeat teardown must consult the latch')
  assert.ok(/primaryTeardownLatch\.child\(\)/.test(teardown), 'repeat teardown must re-target the latched child')
  assert.ok(/if\s*\(confirmed\)/.test(teardown), 'teardown must branch on the confirmed result')
  assert.ok(/primaryTeardownLatch\.clear\(\)/.test(teardown), 'confirmed exit clears the latch')
  assert.ok(/primaryTeardownLatch\.arm\(/.test(teardown), 'unconfirmed exit arms the latch')
  // The arm branch must persist the blocked state, not leave phase 'missing'.
  const armIdx = teardown.indexOf('primaryTeardownLatch.arm(')
  assert.ok(/primaryBackendPhase\s*=\s*'failed'/.test(teardown.slice(armIdx)), 'unconfirmed teardown sets phase failed')

  // startHermes must refuse while the latch is active, BEFORE reusing/creating a
  // connectionPromise or spawning.
  const start = mainSrc.match(/async function startHermes\(\) \{([\s\S]*?)\n  return connectionPromise\n\}/)[1]
  const gateIdx = start.search(/if\s*\(primaryTeardownLatch\.isActive\(\)\)/)
  const promiseIdx = start.indexOf('if (connectionPromise) return connectionPromise')
  assert.ok(gateIdx !== -1, 'startHermes must gate on the unconfirmed-teardown latch')
  assert.ok(promiseIdx !== -1, 'startHermes still has its connectionPromise reuse')
  assert.ok(gateIdx < promiseIdx, 'the latch gate must precede connectionPromise reuse / spawn')
  const gateBody = start.slice(gateIdx, promiseIdx)
  assert.ok(/throw new Error\(/.test(gateBody), 'active latch throws instead of starting')
})

test('primary status reports failed (never missing) while the teardown latch is active', () => {
  const state = mainSrc.match(/function primaryBackendState\(\) \{([\s\S]*?)\n\}/)[1]
  const latchIdx = state.search(/if\s*\(primaryTeardownLatch\.isActive\(\)\)/)
  const fallbackIdx = state.indexOf('return primaryBackendPhase')
  assert.ok(latchIdx !== -1, 'status must consult the latch')
  assert.ok(fallbackIdx !== -1, 'status falls back to the tracked phase')
  assert.ok(latchIdx < fallbackIdx, 'the active-latch failed report precedes the phase fallback')
  const branch = state.slice(latchIdx, fallbackIdx)
  assert.ok(/return\s*'failed'/.test(branch), 'active latch maps to failed')
})

test('a latched primary child clears the latch on its eventual exit', () => {
  assert.ok(
    /function watchLatchedPrimaryChildExit\(child\)/.test(mainSrc),
    'a one-shot exit watcher must exist so a late confirmed exit clears the latch'
  )
  const watch = mainSrc.match(/function watchLatchedPrimaryChildExit\(child\) \{([\s\S]*?)\n\}/)[1]
  assert.ok(/child\.once\('exit'/.test(watch), 'watcher installs a one-shot exit listener')
  assert.ok(/primaryTeardownLatch\.clear\(\)/.test(watch), 'watcher clears the latch on exit')
  assert.ok(/primaryTeardownLatch\.child\(\)\s*===\s*child/.test(watch), 'watcher guards against a newer latch')
})

test('bootstrap repair routes through confirmed teardown and fails closed (no direct reset)', () => {
  const repair = mainSrc.match(/ipcMain\.handle\('hermes:bootstrap:repair', async \(\) => \{([\s\S]*?)\n\}\)/)[1]
  assert.ok(/await teardownPrimaryBackendAndWait\(\)/.test(repair), 'repair must confirm primary teardown')
  // Statement-form only — the body's own comment mentions the name in prose.
  assert.equal(
    /^\s*resetHermesConnection\(\)/m.test(repair),
    false,
    'repair must NOT call resetHermesConnection() directly (bypasses the latch)'
  )
  // Must check the confirmed result and fail closed before removing the marker /
  // clearing latched failures.
  const guardIdx = repair.search(/if\s*\(\s*!confirmed\s*\)/)
  assert.ok(guardIdx !== -1, 'repair must guard on !confirmed')
  assert.ok(/return\s*\{\s*ok:\s*false,\s*error:\s*'teardown-unconfirmed'/.test(repair), 'unconfirmed repair returns fail-closed')
  const markerIdx = repair.indexOf('BOOTSTRAP_COMPLETE_MARKER')
  const clearIdx = repair.indexOf('bootstrapFailure = null')
  assert.ok(guardIdx < markerIdx, 'the !confirmed guard must precede marker removal')
  assert.ok(guardIdx < clearIdx, 'the !confirmed guard must precede clearing latched failures')
})

test('bootstrap reset routes through confirmed teardown and fails closed (no direct reset)', () => {
  const reset = mainSrc.match(/ipcMain\.handle\('hermes:bootstrap:reset', async \(\) => \{([\s\S]*?)\n\}\)/)[1]
  assert.ok(/await teardownPrimaryBackendAndWait\(\)/.test(reset), 'reset must confirm primary teardown')
  assert.equal(/^\s*resetHermesConnection\(\)/m.test(reset), false, 'reset must NOT call resetHermesConnection() directly')
  const guardIdx = reset.search(/if\s*\(\s*!confirmed\s*\)/)
  assert.ok(guardIdx !== -1, 'reset must guard on !confirmed')
  assert.ok(/return\s*\{\s*ok:\s*false,\s*error:\s*'teardown-unconfirmed'/.test(reset), 'unconfirmed reset returns fail-closed')
  const clearIdx = reset.indexOf('bootstrapFailure = null')
  assert.ok(guardIdx < clearIdx, 'the !confirmed guard must precede clearing latched failures')
})

test('direct resetHermesConnection() callers are confined to confirmed-teardown + remote-cache-only paths', () => {
  // Only statement-form calls (trimmed line === the call). This excludes the
  // function definition and the many prose mentions in comments.
  const callSites = mainSrc.match(/^\s*resetHermesConnection\(\)\s*$/gm) || []
  assert.equal(callSites.length, 2, `expected exactly 2 direct resetHermesConnection() call sites, found ${callSites.length}`)

  // Caller 1: the confirmed-teardown helper itself.
  const teardown = mainSrc.match(/async function teardownPrimaryBackendAndWait\(\) \{([\s\S]*?)\n\}/)[1]
  assert.ok(/resetHermesConnection\(\)/.test(teardown), 'teardownPrimaryBackendAndWait is an allowed reset caller')

  // Caller 2: the remote-cache liveness-drop path — remote-only (no local child
  // to leave running), so a direct reset is safe there. Assert the reset sits
  // right after that path's log line.
  const remoteIdx = mainSrc.indexOf('Cached remote Hermes backend failed liveness probe')
  assert.ok(remoteIdx !== -1, 'the remote-cache liveness-drop path must still exist')
  const remoteWindow = mainSrc.slice(remoteIdx, remoteIdx + 260)
  assert.ok(/resetHermesConnection\(\)/.test(remoteWindow), 'remote-cache path is the only other allowed reset caller')

  // Neither bootstrap recovery handler may be a direct reset caller.
  for (const channel of ['hermes:bootstrap:repair', 'hermes:bootstrap:reset']) {
    const body = mainSrc.match(new RegExp(`ipcMain\\.handle\\('${channel}', async \\(\\) => \\{([\\s\\S]*?)\\n\\}\\)`))[1]
    assert.equal(/^\s*resetHermesConnection\(\)/m.test(body), false, `${channel} must not directly reset the primary connection`)
  }
})

// ── M4a: session backend spawn substrate (static guards) ─────────────────

test('session spawn env carries MCP proxy mode + session id, gated to session scope', () => {
  // Proxy mode prevents duplicate stdio MCP; session id is internal metadata.
  assert.ok(mainSrc.includes("isSession ? { HERMES_MCP_MODE: 'proxy' }"), 'session backends must run MCP in proxy mode')
  assert.ok(
    mainSrc.includes('isSession ? { HERMES_DESKTOP_BACKEND_SESSION_ID: sessionId }'),
    'session id env is set only for session scope'
  )
})

test('every spawn-path registry write uses the scope variable — no hardcoded profile scope', () => {
  assert.equal(/scope:\s*'profile'/.test(mainSrc), false, 'spawn-path records must use the scope variable, not a literal')
  assert.ok(
    mainSrc.includes('backendDescriptorId({ scope, profile, sessionId })'),
    'the registry id must be derived from the real scope/profile/sessionId'
  )
  assert.ok(mainSrc.includes('HERMES_DESKTOP_BACKEND_SCOPE: scope'), 'the scope env must reflect the real scope')
})

test('the dashboard argv carries only the non-secret owner nonce — no tokens', () => {
  const match = mainSrc.match(/const dashboardArgs = \[[^\]]*\]/)
  assert.ok(match, 'dashboardArgs literal should be present')
  assert.ok(/backendOwnerArg\(ownerNonce\)/.test(match[0]), 'argv carries the non-secret owner nonce')
  assert.equal(/token/i.test(match[0]), false, 'no token of any kind may be placed in argv')
})

test('session backends reserve via the fail-closed reservation wrapper (R13)', () => {
  // Session reservation uses the same reserveThenSpawn + reserveDesktopBackendRegistryRecord
  // path as profile scope, so a conflict/write-failure fails closed before spawn.
  assert.ok(mainSrc.includes('reserve: () => reserveDesktopBackendRegistryRecord({'))
  assert.ok(mainSrc.includes('onReservationFailed'))
})

test('child exit/error handlers forget by the pool KEY (scopeKey), never by profile', () => {
  // A session backend's pool key is `session:<profile>:<sessionId>`. Forgetting by
  // `profile` would delete the sibling PROFILE entry and strand the session entry.
  // Every forgetBackend call in the spawn path must use the scope key.
  const forgetCalls = mainSrc.match(/profileBackendPool\.forgetBackend\([^)]*\)/g) || []
  assert.ok(forgetCalls.length >= 2, 'spawn path should forget on both error and exit')
  for (const call of forgetCalls) {
    assert.ok(/forgetBackend\(scopeKey\)/.test(call), `forget must use scopeKey, saw: ${call}`)
  }
  assert.equal(/forgetBackend\(profile\)/.test(mainSrc), false, 'must never forget by profile')
})
