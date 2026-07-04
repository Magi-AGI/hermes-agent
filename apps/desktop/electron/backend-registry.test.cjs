const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')

const {
  PROCESS_START_TIME_TOLERANCE_SECONDS,
  backendDescriptorId,
  backendOwnerArg,
  buildProcessTableFromRows,
  desktopBackendRegistryPath,
  hasSecretLikeRegistryField,
  normalizeProcessStartTime,
  readBackendRegistry,
  reconcileBackendRegistry,
  registryRecordForDescriptor,
  verifyBackendOwnership,
  writeBackendRegistry
} = require('./backend-registry.cjs')

function mkTmp() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-backend-registry-test-'))
}

test('registry path is rooted at Electron Hermes home, not profile HERMES_HOME', () => {
  const root = path.join(mkTmp(), 'hermes-root')
  const profileHome = path.join(root, 'profiles', 'claudetriad')

  assert.equal(desktopBackendRegistryPath(root), path.join(root, 'runtime', 'desktop-backends.json'))
  assert.equal(desktopBackendRegistryPath(profileHome), path.join(root, 'runtime', 'desktop-backends.json'))
})

test('backend descriptors have stable profile/session identifiers', () => {
  assert.equal(backendDescriptorId({ scope: 'profile', profile: 'claudetriad' }), 'profile:claudetriad')
  assert.equal(
    backendDescriptorId({ scope: 'session', profile: 'claudetriad', sessionId: 'abc123' }),
    'session:claudetriad:abc123'
  )
  assert.throws(() => backendDescriptorId({ scope: 'session', profile: 'claudetriad' }), /sessionId/)
})

test('registry records redact runtime tokens and reject secret-like fields', () => {
  const descriptor = {
    scope: 'profile',
    profile: 'claudetriad',
    id: 'profile:claudetriad',
    baseUrl: 'http://127.0.0.1:1234',
    token: 'runtime-token-must-not-persist',
    authToken: 'runtime-token-must-not-persist',
    pid: 123,
    processStartTime: 456,
    installRoot: '/repo',
    lifecycle: 'ready'
  }

  const record = registryRecordForDescriptor(descriptor, {
    desktopInstanceId: 'desktop-1',
    ownerNonce: 'nonce-1',
    startedAt: '2026-07-02T00:00:00Z'
  })

  assert.equal(record.token, undefined)
  assert.equal(record.authToken, undefined)
  assert.equal(record.baseUrl, undefined)
  assert.equal(hasSecretLikeRegistryField(record), false)
  assert.equal(hasSecretLikeRegistryField({ ...record, brokerToken: 'bad' }), true)
})

test('registry read/write ignores corrupt files safely', () => {
  const dir = mkTmp()
  const file = path.join(dir, 'runtime', 'desktop-backends.json')

  fs.mkdirSync(path.dirname(file), { recursive: true })
  fs.writeFileSync(file, '{not json')
  assert.deepEqual(readBackendRegistry(file), { records: [], corrupt: true })

  writeBackendRegistry(file, [{ id: 'profile:claudetriad', pid: 123 }])
  assert.deepEqual(readBackendRegistry(file), { records: [{ id: 'profile:claudetriad', pid: 123 }], corrupt: false })
})

test('ownership verification fails closed unless pid/create-time/install-root and observable nonce match', () => {
  const record = {
    pid: 123,
    processStartTime: 1000,
    installRoot: '/repo',
    ownerNonce: 'owner-1',
    sidecarPath: '/tmp/sidecar.json'
  }

  assert.deepEqual(
    verifyBackendOwnership(record, null, { expectedInstallRoot: '/repo' }),
    { killable: false, reason: 'missing_record_or_process' }
  )
  assert.deepEqual(
    verifyBackendOwnership(record, { pid: 123, processStartTime: 900, installRoot: '/repo', argv: [backendOwnerArg('owner-1')] }, { expectedInstallRoot: '/repo' }),
    { killable: false, reason: 'pid_reuse_start_time_mismatch' }
  )
  assert.deepEqual(
    verifyBackendOwnership(record, { pid: 123, processStartTime: 1000, installRoot: '/other', argv: [backendOwnerArg('owner-1')] }, { expectedInstallRoot: '/repo' }),
    { killable: false, reason: 'install_root_mismatch' }
  )
  assert.deepEqual(
    verifyBackendOwnership(record, { pid: 123, processStartTime: 1000, installRoot: '/repo', argv: ['hermes', 'dashboard'] }, { expectedInstallRoot: '/repo' }),
    { killable: false, reason: 'missing_observable_nonce' }
  )
  assert.deepEqual(
    verifyBackendOwnership(record, { pid: 123, processStartTime: 1000, installRoot: '/repo', argv: [backendOwnerArg('owner-1')] }, { expectedInstallRoot: '/repo' }),
    { killable: true, reason: 'argv_nonce_verified' }
  )
  assert.deepEqual(
    verifyBackendOwnership(record, { pid: 123, processStartTime: 1000, installRoot: '/repo', argv: [] }, { expectedInstallRoot: '/repo', sidecars: { '/tmp/sidecar.json': { kind: 'hermes-desktop-backend', ownerNonce: 'owner-1' } } }),
    { killable: true, reason: 'sidecar_nonce_verified' }
  )
})

test('create-time comparison tolerates OS/float precision but fails closed on genuine mismatch', () => {
  const base = {
    pid: 123,
    installRoot: '/repo',
    ownerNonce: 'owner-1',
    sidecarPath: '/tmp/sidecar.json'
  }
  const opts = { expectedInstallRoot: '/repo' }
  const argv = [backendOwnerArg('owner-1')]

  // Exact match on a realistic psutil epoch-seconds float.
  assert.deepEqual(
    verifyBackendOwnership({ ...base, processStartTime: 1751414400.123456 }, { pid: 123, processStartTime: 1751414400.123456, installRoot: '/repo', argv }, opts),
    { killable: true, reason: 'argv_nonce_verified' }
  )

  // Sub-second/rounding drift within tolerance is accepted.
  assert.deepEqual(
    verifyBackendOwnership({ ...base, processStartTime: 1751414400.9 }, { pid: 123, processStartTime: 1751414400.1, installRoot: '/repo', argv }, opts),
    { killable: true, reason: 'argv_nonce_verified' }
  )

  // A difference beyond tolerance (PID reuse) fails closed.
  assert.deepEqual(
    verifyBackendOwnership({ ...base, processStartTime: 1751414400 }, { pid: 123, processStartTime: 1751414400 + PROCESS_START_TIME_TOLERANCE_SECONDS + 5, installRoot: '/repo', argv }, opts),
    { killable: false, reason: 'pid_reuse_start_time_mismatch' }
  )

  // A millisecond-scale source normalizes to seconds before comparison, so an
  // epoch-ms record and an epoch-seconds proc reading of the same instant match.
  assert.deepEqual(
    verifyBackendOwnership({ ...base, processStartTime: 1751414400123 }, { pid: 123, processStartTime: 1751414400.123, installRoot: '/repo', argv }, opts),
    { killable: true, reason: 'argv_nonce_verified' }
  )

  // A missing create-time on either side skips the start-time gate (nonce still required).
  assert.deepEqual(
    verifyBackendOwnership({ ...base, processStartTime: null }, { pid: 123, installRoot: '/repo', argv }, opts),
    { killable: true, reason: 'argv_nonce_verified' }
  )
})

test('normalizeProcessStartTime coerces units and rejects junk', () => {
  assert.equal(normalizeProcessStartTime(null), null)
  assert.equal(normalizeProcessStartTime(undefined), null)
  assert.equal(normalizeProcessStartTime('not-a-number'), null)
  assert.equal(normalizeProcessStartTime(1751414400.5), 1751414400.5)
  assert.equal(normalizeProcessStartTime(1751414400500), 1751414400.5)
})

test('the exact argv marker Desktop spawns verifies ownership, and env-only does not', () => {
  const nonce = 'owner-nonce-xyz'
  const record = {
    pid: 900,
    processStartTime: 1751414400,
    installRoot: '/repo',
    ownerNonce: nonce,
    sidecarPath: '/tmp/s.json'
  }
  const opts = { expectedInstallRoot: '/repo' }
  const base = { pid: 900, processStartTime: 1751414400, installRoot: '/repo' }

  // Desktop builds the marker with backendOwnerArg(nonce); it must land as a
  // single argv token that verification accepts (defense-in-depth path).
  const marker = backendOwnerArg(nonce)
  assert.equal(marker, `--hermes-desktop-owner=${nonce}`)
  assert.deepEqual(
    verifyBackendOwnership(record, { ...base, argv: ['python', '-m', 'hermes_cli', marker, 'dashboard'] }, opts),
    { killable: true, reason: 'argv_nonce_verified' }
  )

  // Env-only ownership (nonce present in the child env, but NOT on argv and no
  // sidecar) is NOT sufficient — the reaper can't read child env post-restart.
  assert.deepEqual(
    verifyBackendOwnership(record, { ...base, argv: ['python', '-m', 'hermes_cli', 'dashboard'] }, opts),
    { killable: false, reason: 'missing_observable_nonce' }
  )

  // The sidecar remains the authoritative/primary path: it verifies even when
  // the argv marker is absent.
  assert.deepEqual(
    verifyBackendOwnership(
      record,
      { ...base, argv: ['python', '-m', 'hermes_cli', 'dashboard'] },
      { ...opts, sidecars: { '/tmp/s.json': { kind: 'hermes-desktop-backend', ownerNonce: nonce } } }
    ),
    { killable: true, reason: 'sidecar_nonce_verified' }
  )
})

test('registry reconciliation prunes dead records and only kills verified hung backends', () => {
  const records = [
    { id: 'dead', pid: 1, processStartTime: 1, installRoot: '/repo', ownerNonce: 'dead', lifecycle: 'ready' },
    { id: 'manual', pid: 2, processStartTime: 2, installRoot: '/repo', ownerNonce: 'manual', lifecycle: 'unresponsive' },
    { id: 'owned', pid: 3, processStartTime: 3, installRoot: '/repo', ownerNonce: 'owned', lifecycle: 'unresponsive' }
  ]
  const processes = {
    2: { pid: 2, processStartTime: 2, installRoot: '/repo', argv: ['hermes', 'dashboard', '--profile', 'claudetriad'] },
    3: { pid: 3, processStartTime: 3, installRoot: '/repo', argv: [backendOwnerArg('owned')], children: [30, 31] }
  }

  const actions = reconcileBackendRegistry(records, processes, { expectedInstallRoot: '/repo' })
  assert.deepEqual(actions.map(action => action.action), ['prune', 'keep_fail_closed', 'kill_tree'])
  assert.equal(actions[1].reason, 'missing_observable_nonce')
  assert.deepEqual(actions[2].children, [30, 31])
})

test('reconcile reaps verified prior-instance orphans but spares current instance and unproven processes', () => {
  const records = [
    // Verified-owned, healthy, but from a PRIOR Desktop instance => orphan.
    { id: 'orphan', pid: 10, processStartTime: 100, installRoot: '/repo', ownerNonce: 'n-orphan', desktopInstanceId: 'old-instance', lifecycle: 'ready' },
    // Current instance, healthy, not claimed => keep (instance matches).
    { id: 'current', pid: 11, processStartTime: 110, installRoot: '/repo', ownerNonce: 'n-current', desktopInstanceId: 'this-instance', lifecycle: 'ready' },
    // Current instance, claimed by the live pool => keep regardless of lifecycle.
    { id: 'claimed', pid: 12, processStartTime: 120, installRoot: '/repo', ownerNonce: 'n-claimed', desktopInstanceId: 'this-instance', lifecycle: 'unresponsive' },
    // Prior instance BUT not Desktop-owned (manual dashboard, no nonce) => fail closed.
    { id: 'manual', pid: 13, processStartTime: 130, installRoot: '/repo', ownerNonce: 'n-manual', desktopInstanceId: 'old-instance', lifecycle: 'ready' },
    // Prior instance, owned, but its cmdline is unreadable => fail closed.
    { id: 'locked', pid: 14, processStartTime: 140, installRoot: '/repo', ownerNonce: 'n-locked', desktopInstanceId: 'old-instance', lifecycle: 'ready' }
  ]
  const processes = {
    10: { pid: 10, processStartTime: 100, installRoot: '/repo', argv: [backendOwnerArg('n-orphan')], children: [101] },
    11: { pid: 11, processStartTime: 110, installRoot: '/repo', argv: [backendOwnerArg('n-current')] },
    12: { pid: 12, processStartTime: 120, installRoot: '/repo', argv: [backendOwnerArg('n-claimed')] },
    13: { pid: 13, processStartTime: 130, installRoot: '/repo', argv: ['hermes', 'dashboard', '--profile', 'claudetriad'] },
    14: { pid: 14, processStartTime: 140, installRoot: '/repo', argv: [], cmdlineReadable: false }
  }

  const actions = reconcileBackendRegistry(records, processes, {
    expectedInstallRoot: '/repo',
    currentInstanceId: 'this-instance',
    claimedIds: ['claimed']
  })
  const byId = Object.fromEntries(actions.map(action => [action.id, action]))

  assert.deepEqual(byId.orphan, { action: 'kill_tree', id: 'orphan', pid: 10, children: [101], reason: 'prior_instance_orphan' })
  assert.equal(byId.current.action, 'keep')
  assert.equal(byId.current.reason, 'verified_alive')
  assert.equal(byId.claimed.action, 'keep')
  assert.equal(byId.claimed.reason, 'claimed_current_instance')
  assert.equal(byId.manual.action, 'keep_fail_closed')
  assert.equal(byId.manual.reason, 'missing_observable_nonce')
  assert.equal(byId.locked.action, 'keep_fail_closed')
  assert.equal(byId.locked.reason, 'cmdline_unreadable')
})

test('buildProcessTableFromRows normalizes create-time, groups children, and gates install root on cmdline', () => {
  const rows = [
    { pid: 10, ppid: 1, createTime: 1751414400, cmdline: 'C:/repo/venv/python.exe -m hermes dashboard --hermes-desktop-owner=n1', cmdlineReadable: true },
    { pid: 20, ppid: 10, createTime: 1751414400500, cmdline: 'C:/repo/venv/hermes.exe worker', cmdlineReadable: true },
    { pid: 30, ppid: 10, createTime: 1751414401, cmdline: 'C:/elsewhere/python.exe -m hermes', cmdlineReadable: true },
    { pid: 40, ppid: 1, createTime: null, cmdline: '', cmdlineReadable: false }
  ]

  const table = buildProcessTableFromRows(rows, { expectedInstallRoot: 'C:/repo' })

  // create-time normalized to seconds (ms row divided down).
  assert.equal(table['10'].processStartTime, 1751414400)
  assert.equal(table['20'].processStartTime, 1751414400.5)
  // children grouped by ppid.
  assert.deepEqual(table['10'].children.sort((a, b) => a - b), [20, 30])
  // argv split carries the owner-nonce marker for verification.
  assert.ok(table['10'].argv.includes('--hermes-desktop-owner=n1'))
  // install root claimed only when the cmdline evidences it.
  assert.equal(table['10'].installRoot, 'C:/repo')
  assert.equal(table['30'].installRoot, null)
  // unreadable/empty cmdline stays unreadable so ownership fails closed.
  assert.equal(table['40'].cmdlineReadable, false)
  assert.equal(table['40'].installRoot, null)
})
