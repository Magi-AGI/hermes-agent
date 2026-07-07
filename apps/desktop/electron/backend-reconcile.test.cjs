const test = require('node:test')
const assert = require('node:assert/strict')

const { createBackendReconciler } = require('./backend-reconcile.cjs')
const { backendOwnerArg } = require('./backend-registry.cjs')
const { createRegistryStore } = require('./backend-registry-store.cjs')

function makeReconciler(overrides = {}) {
  const killed = []
  const writes = [] // post-commit registry snapshots (what the file would hold)
  const removedSidecars = []
  // killResults: optional map of pid -> boolean (confirmed gone). Default true.
  const killResults = overrides.killResults || {}
  // Back the reconciler with the REAL generation-safe store over an in-memory
  // registry, so the commit path (nonce matching, removal, tombstone) is
  // exercised exactly as production would.
  let state = (overrides.records || []).map(r => ({ ...r }))
  const store = createRegistryStore({
    readRecords: () => state.map(r => ({ ...r })),
    writeRecords: records => {
      state = records.map(r => ({ ...r }))
      writes.push(state.map(r => ({ ...r })))
    }
  })
  const deps = {
    readRegistry: () => ({ records: state.map(r => ({ ...r })) }),
    commitReconcile: decisions => store.commitReconcile(decisions),
    enumerateProcessRows: overrides.enumerateProcessRows
      ? () => overrides.enumerateProcessRows({ store, state })
      : (() => overrides.rows || []),
    killBackendTree: pid => {
      killed.push(pid)
      return Object.prototype.hasOwnProperty.call(killResults, pid) ? killResults[pid] : true
    },
    collectClaimedIds: () => overrides.claimedIds || [],
    loadSidecars: () => overrides.sidecars || {},
    removeSidecar: sidecarPath => removedSidecars.push(sidecarPath),
    expectedInstallRoot: '/repo',
    currentInstanceId: 'this-instance',
    now: () => 1000,
    log: () => undefined
  }
  return { reconciler: createBackendReconciler(deps), killed, writes, removedSidecars, store, finalRecords: () => state }
}

test('reconcile reaps a prior-instance orphan and rewrites the registry without it', async () => {
  const records = [
    { id: 'orphan', pid: 10, processStartTime: 100, installRoot: '/repo', ownerNonce: 'n1', desktopInstanceId: 'old', lifecycle: 'ready', sidecarPath: '/s/orphan.json' },
    { id: 'current', pid: 11, processStartTime: 110, installRoot: '/repo', ownerNonce: 'n2', desktopInstanceId: 'this-instance', lifecycle: 'ready', sidecarPath: '/s/current.json' }
  ]
  const rows = [
    { pid: 10, ppid: 1, createTime: 100, cmdline: `/repo/venv/python ${backendOwnerArg('n1')}`, cmdlineReadable: true },
    { pid: 20, ppid: 10, createTime: 100, cmdline: '/repo/venv/hermes worker', cmdlineReadable: true },
    { pid: 11, ppid: 1, createTime: 110, cmdline: `/repo/venv/python ${backendOwnerArg('n2')}`, cmdlineReadable: true }
  ]
  const { reconciler, killed, writes, removedSidecars } = makeReconciler({ records, rows })

  const result = await reconciler.reconcile('startup')

  assert.deepEqual(result, { reaped: 1, pruned: 0, tombstoned: 0, kept: 1 })
  assert.ok(killed.includes(10), 'orphan root killed')
  assert.ok(killed.includes(20), 'orphan child killed')
  assert.ok(!killed.includes(11), 'current-instance backend spared')
  assert.equal(writes.length, 1)
  assert.deepEqual(writes[0].map(record => record.id), ['current'])
  assert.deepEqual(removedSidecars, ['/s/orphan.json'])
})

test('reconcile tombstones an orphan whose kill is unconfirmed and keeps its sidecar', async () => {
  const records = [
    { id: 'orphan', pid: 10, processStartTime: 100, installRoot: '/repo', ownerNonce: 'n1', desktopInstanceId: 'old', lifecycle: 'ready', sidecarPath: '/s/orphan.json' },
    { id: 'current', pid: 11, processStartTime: 110, installRoot: '/repo', ownerNonce: 'n2', desktopInstanceId: 'this-instance', lifecycle: 'ready', sidecarPath: '/s/current.json' }
  ]
  const rows = [
    { pid: 10, ppid: 1, createTime: 100, cmdline: `/repo/venv/python ${backendOwnerArg('n1')}`, cmdlineReadable: true },
    { pid: 11, ppid: 1, createTime: 110, cmdline: `/repo/venv/python ${backendOwnerArg('n2')}`, cmdlineReadable: true }
  ]
  // Kill does NOT confirm the tree is gone.
  const { reconciler, writes, removedSidecars } = makeReconciler({ records, rows, killResults: { 10: false } })

  const result = await reconciler.reconcile('startup')

  assert.deepEqual(result, { reaped: 0, pruned: 0, tombstoned: 1, kept: 1 })
  // Registry rewritten: orphan row RETAINED but tombstoned; current untouched.
  assert.equal(writes.length, 1)
  const rewritten = Object.fromEntries(writes[0].map(record => [record.id, record]))
  assert.equal(rewritten.orphan.lifecycle, 'failed')
  assert.equal(rewritten.orphan.tombstoneReason, 'prior_instance_orphan')
  assert.equal(rewritten.orphan.tombstonedAt, 1000)
  assert.equal(rewritten.current.lifecycle, 'ready')
  // Sidecar for the un-killed orphan is KEPT so ownership re-verifies next pass.
  assert.deepEqual(removedSidecars, [])
})

test('a best-effort kill returning undefined is NOT treated as confirmed (tombstones)', async () => {
  // The production main.cjs adapter historically returned undefined; the reaper
  // must require an explicit true, else it would drop records for survivors.
  const records = [
    { id: 'orphan', pid: 10, processStartTime: 100, installRoot: '/repo', ownerNonce: 'n1', desktopInstanceId: 'old', lifecycle: 'ready', sidecarPath: '/s/orphan.json' }
  ]
  const rows = [{ pid: 10, ppid: 1, createTime: 100, cmdline: `/repo/venv/python ${backendOwnerArg('n1')}`, cmdlineReadable: true }]
  const { reconciler, writes, removedSidecars } = makeReconciler({ records, rows, killResults: { 10: undefined } })

  const result = await reconciler.reconcile('startup')

  assert.deepEqual(result, { reaped: 0, pruned: 0, tombstoned: 1, kept: 0 })
  const rewritten = Object.fromEntries(writes[0].map(record => [record.id, record]))
  assert.equal(rewritten.orphan.lifecycle, 'failed')
  assert.deepEqual(removedSidecars, [])
})

test('a tombstoned orphan is reaped on the next pass once its kill confirms', async () => {
  const records = [
    { id: 'orphan', pid: 10, processStartTime: 100, installRoot: '/repo', ownerNonce: 'n1', desktopInstanceId: 'old', lifecycle: 'failed', tombstoneReason: 'prior_instance_orphan', sidecarPath: '/s/orphan.json' }
  ]
  const rows = [{ pid: 10, ppid: 1, createTime: 100, cmdline: `/repo/venv/python ${backendOwnerArg('n1')}`, cmdlineReadable: true }]
  const { reconciler, killed, writes, removedSidecars } = makeReconciler({ records, rows, killResults: { 10: true } })

  const result = await reconciler.reconcile('startup')

  assert.deepEqual(result, { reaped: 1, pruned: 0, tombstoned: 0, kept: 0 })
  assert.ok(killed.includes(10))
  assert.deepEqual(writes[0], [])
  assert.deepEqual(removedSidecars, ['/s/orphan.json'])
})

test('reconcile aborts (no kills, no writes) when process enumeration fails', async () => {
  const records = [{ id: 'orphan', pid: 10, processStartTime: 100, installRoot: '/repo', ownerNonce: 'n1', desktopInstanceId: 'old', lifecycle: 'ready' }]
  const { reconciler, killed, writes } = makeReconciler({
    records,
    enumerateProcessRows: () => {
      throw new Error('CIM unavailable')
    }
  })

  const result = await reconciler.reconcile('startup')

  assert.equal(result, null)
  assert.deepEqual(killed, [])
  assert.deepEqual(writes, [])
})

test('reconcile is a no-op on an empty registry and never enumerates', async () => {
  let enumerated = false
  const { reconciler, killed, writes } = makeReconciler({
    records: [],
    enumerateProcessRows: () => {
      enumerated = true
      return []
    }
  })

  const result = await reconciler.reconcile('startup')

  assert.deepEqual(result, { reaped: 0, pruned: 0, tombstoned: 0, kept: 0 })
  assert.equal(enumerated, false)
  assert.deepEqual(killed, [])
  assert.deepEqual(writes, [])
})

test('reconcile prunes a dead row (no live process) and drops its sidecar', async () => {
  const records = [{ id: 'dead', pid: 99, processStartTime: 5, installRoot: '/repo', ownerNonce: 'n', desktopInstanceId: 'old', lifecycle: 'ready', sidecarPath: '/s/dead.json' }]
  const { reconciler, killed, writes, removedSidecars } = makeReconciler({ records, rows: [] })

  const result = await reconciler.reconcile('startup')

  assert.deepEqual(result, { reaped: 0, pruned: 1, tombstoned: 0, kept: 0 })
  assert.deepEqual(killed, [])
  assert.deepEqual(writes[0], [])
  assert.deepEqual(removedSidecars, ['/s/dead.json'])
})

test('reconcile does not clobber a fresh same-id row upserted while it was deciding', async () => {
  // Snapshot sees the OLD orphan (nonce n1). Mid-enumeration a fresh backend
  // re-claims the same id with a NEW nonce (as a same-id restart would). The
  // reaper's stale remove decision must NOT delete the fresh row/sidecar.
  const records = [
    { id: 'orphan', pid: 10, processStartTime: 100, installRoot: '/repo', ownerNonce: 'n1', desktopInstanceId: 'old', lifecycle: 'ready', sidecarPath: '/s/orphan.json' }
  ]
  const { reconciler, writes, removedSidecars, finalRecords } = makeReconciler({
    records,
    enumerateProcessRows: ({ store }) => {
      // Fresh spawn lands (queued on the store) before the reconcile commit.
      store.upsert({ id: 'orphan', pid: 10, processStartTime: 100, installRoot: '/repo', ownerNonce: 'n-fresh', desktopInstanceId: 'this-instance', lifecycle: 'ready', sidecarPath: '/s/orphan.json' })
      return [{ pid: 10, ppid: 1, createTime: 100, cmdline: `/repo/venv/python ${backendOwnerArg('n1')}`, cmdlineReadable: true }]
    }
  })

  const result = await reconciler.reconcile('startup')

  // The stale remove (nonce n1) matched nothing at commit time → nothing reaped.
  assert.equal(result.reaped, 0)
  const finalById = Object.fromEntries(finalRecords().map(r => [r.id, r]))
  assert.equal(finalById.orphan.ownerNonce, 'n-fresh', 'fresh same-id row survived')
  // The fresh backend's sidecar (same deterministic path) was NOT unlinked.
  assert.deepEqual(removedSidecars, [])
  // Only the fresh upsert wrote; the commit found nothing to change.
  assert.equal(writes.length, 1)
})
