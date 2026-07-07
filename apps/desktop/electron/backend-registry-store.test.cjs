const test = require('node:test')
const assert = require('node:assert/strict')

const { createRegistryStore } = require('./backend-registry-store.cjs')

// In-memory registry backing whose read/write can be instrumented to simulate
// interleaving. `gate` (when set) makes readRecords await a promise the test
// controls, emulating the async gap the real reaper has around enumeration.
function makeBacking(initial = []) {
  const state = { records: initial.map(r => ({ ...r })) }
  return {
    state,
    readRecords: () => state.records.map(r => ({ ...r })),
    writeRecords: records => {
      state.records = records.map(r => ({ ...r }))
    }
  }
}

test('mutations serialize: last upsert of the same id wins in order', async () => {
  const backing = makeBacking()
  const store = createRegistryStore(backing)

  await Promise.all([
    store.upsert({ id: 'profile:a', ownerNonce: 'n1', lifecycle: 'starting' }),
    store.upsert({ id: 'profile:a', ownerNonce: 'n2', lifecycle: 'ready' })
  ])
  await store.drain()

  assert.equal(backing.state.records.length, 1)
  assert.equal(backing.state.records[0].ownerNonce, 'n2')
  assert.equal(backing.state.records[0].lifecycle, 'ready')
})

test('commitReconcile does NOT clobber a fresh same-id row upserted after the snapshot', async () => {
  // Reaper decided to remove the OLD generation (nonce old), but by commit time
  // a fresh backend re-used the id with a new nonce. The fresh row must survive.
  const backing = makeBacking([{ id: 'profile:a', ownerNonce: 'new', lifecycle: 'ready', sidecarPath: '/s/a.json' }])
  const store = createRegistryStore(backing)

  const { removedIds } = await store.commitReconcile({
    removeTargets: [{ id: 'profile:a', ownerNonce: 'old' }]
  })
  await store.drain()

  assert.deepEqual(removedIds, [])
  assert.equal(backing.state.records.length, 1)
  assert.equal(backing.state.records[0].ownerNonce, 'new')
})

test('commitReconcile removes only rows whose nonce still matches', async () => {
  const backing = makeBacking([
    { id: 'profile:a', ownerNonce: 'old', lifecycle: 'failed' },
    { id: 'profile:b', ownerNonce: 'keep', lifecycle: 'ready' }
  ])
  const store = createRegistryStore(backing)

  const { removedIds } = await store.commitReconcile({
    removeTargets: [{ id: 'profile:a', ownerNonce: 'old' }, { id: 'profile:b', ownerNonce: 'stale' }]
  })
  await store.drain()

  assert.deepEqual(removedIds, ['profile:a'])
  assert.deepEqual(backing.state.records.map(r => r.id), ['profile:b'])
})

test('commitReconcile tombstones matching rows and reports them, keeping the row', async () => {
  const backing = makeBacking([{ id: 'profile:a', ownerNonce: 'n', lifecycle: 'ready' }])
  const store = createRegistryStore(backing)

  const { tombstonedIds, removedIds } = await store.commitReconcile({
    tombstoneTargets: [{ id: 'profile:a', ownerNonce: 'n', patch: { lifecycle: 'failed', tombstonedAt: 5 } }]
  })
  await store.drain()

  assert.deepEqual(removedIds, [])
  assert.deepEqual(tombstonedIds, ['profile:a'])
  assert.equal(backing.state.records[0].lifecycle, 'failed')
  assert.equal(backing.state.records[0].tombstonedAt, 5)
})

test('remove with a stale nonce leaves a fresh same-id row (and its sidecar) intact', async () => {
  const backing = makeBacking([{ id: 'profile:a', ownerNonce: 'fresh', sidecarPath: '/s/a.json' }])
  const store = createRegistryStore(backing)

  const res = await store.remove('profile:a', { ownerNonce: 'stale' })
  await store.drain()

  assert.deepEqual(res, { removed: false })
  assert.equal(backing.state.records.length, 1)
})

test('tombstone with a stale nonce does not tombstone a fresh row', async () => {
  const backing = makeBacking([{ id: 'profile:a', ownerNonce: 'fresh', lifecycle: 'ready' }])
  const store = createRegistryStore(backing)

  const res = await store.tombstone('profile:a', { ownerNonce: 'stale', patch: { lifecycle: 'failed' } })
  await store.drain()

  assert.deepEqual(res, { tombstoned: false })
  assert.equal(backing.state.records[0].lifecycle, 'ready')
})

test('upsert refuses to clobber a tombstoned prior generation of a different nonce', async () => {
  const backing = makeBacking([{ id: 'profile:a', ownerNonce: 'old', lifecycle: 'failed', sidecarPath: '/s/a.json' }])
  const store = createRegistryStore(backing)

  const res = await store.upsert({ id: 'profile:a', ownerNonce: 'new', lifecycle: 'starting' })
  await store.drain()

  assert.deepEqual(res, { upserted: false, conflict: true, id: 'profile:a' })
  // The tombstone survives untouched so the reaper can still clear the old gen.
  assert.equal(backing.state.records.length, 1)
  assert.equal(backing.state.records[0].ownerNonce, 'old')
  assert.equal(backing.state.records[0].lifecycle, 'failed')
})

test('upsert replaces a tombstoned row of the SAME nonce (its own generation)', async () => {
  const backing = makeBacking([{ id: 'profile:a', ownerNonce: 'n', lifecycle: 'failed' }])
  const store = createRegistryStore(backing)

  const res = await store.upsert({ id: 'profile:a', ownerNonce: 'n', lifecycle: 'ready' })
  await store.drain()

  assert.deepEqual(res, { upserted: true, id: 'profile:a' })
  assert.equal(backing.state.records[0].lifecycle, 'ready')
})

test('upsert replaces a non-tombstoned (ready) row of a different nonce — normal fresh spawn', async () => {
  const backing = makeBacking([{ id: 'profile:a', ownerNonce: 'old', lifecycle: 'ready' }])
  const store = createRegistryStore(backing)

  const res = await store.upsert({ id: 'profile:a', ownerNonce: 'new', lifecycle: 'starting' })
  await store.drain()

  assert.deepEqual(res, { upserted: true, id: 'profile:a' })
  assert.equal(backing.state.records[0].ownerNonce, 'new')
})

test('connection-config-style unconfirmed stop tombstones, and a same-profile reopen is refused', async () => {
  const backing = makeBacking([{ id: 'profile:x', ownerNonce: 'gen1', lifecycle: 'ready', sidecarPath: '/s/x.json' }])
  const store = createRegistryStore(backing)

  // Confirmed teardown could NOT confirm exit → the pool's tombstoneEntry writes
  // a 'failed' tombstone for the old generation (row + sidecar retained).
  const t = await store.tombstone('profile:x', { ownerNonce: 'gen1', patch: { lifecycle: 'failed', tombstonedAt: 1 } })
  await store.drain()
  assert.deepEqual(t, { tombstoned: true })
  assert.equal(backing.state.records[0].lifecycle, 'failed')

  // Reopen the same profile: the fresh generation (gen2) reserves the id — the
  // store refuses to clobber the un-reaped tombstone, so the old generation stays
  // discoverable for the reaper and the reopen fails clearly/retries.
  const reopen = await store.upsert({ id: 'profile:x', ownerNonce: 'gen2', lifecycle: 'starting' })
  await store.drain()
  assert.deepEqual(reopen, { upserted: false, conflict: true, id: 'profile:x' })
  assert.equal(backing.state.records[0].ownerNonce, 'gen1')
  assert.equal(backing.state.records[0].lifecycle, 'failed')
})

test('a CONFIRMED connection-config stop (row removed) permits a same-profile respawn', async () => {
  const backing = makeBacking([{ id: 'profile:x', ownerNonce: 'gen1', lifecycle: 'ready', sidecarPath: '/s/x.json' }])
  const store = createRegistryStore(backing)

  // Confirmed exit → the exit handler removes the row (nonce-guarded).
  const removed = await store.remove('profile:x', { ownerNonce: 'gen1' })
  await store.drain()
  assert.deepEqual(removed, { removed: true })

  // Reopen: no lingering row → the fresh reservation succeeds.
  const reopen = await store.upsert({ id: 'profile:x', ownerNonce: 'gen2', lifecycle: 'starting' })
  await store.drain()
  assert.deepEqual(reopen, { upserted: true, id: 'profile:x' })
  assert.equal(backing.state.records[0].ownerNonce, 'gen2')
})

test('a writeRecords failure surfaces as a rejected upsert (reservation can fail closed)', async () => {
  // The reservation wrapper relies on this: a store-write error must REJECT (not
  // silently succeed), so the caller can return a non-success reservation and
  // reserveThenSpawn fails closed instead of spawning a recordless child.
  const store = createRegistryStore({
    readRecords: () => [],
    writeRecords: () => {
      throw new Error('disk full')
    }
  })

  await assert.rejects(() => store.upsert({ id: 'profile:a', ownerNonce: 'n', lifecycle: 'starting' }), /disk full/)
})

test('interleaving: an upsert that lands during a queued reconcile is not lost', async () => {
  // Simulate the real ordering: reconcile decision was made on an OLD snapshot,
  // then a fresh spawn upsert is queued, then the reconcile commit runs. Because
  // the commit re-reads under the lock and matches by nonce, the fresh row wins.
  const backing = makeBacking([{ id: 'profile:a', ownerNonce: 'old', lifecycle: 'ready' }])
  const store = createRegistryStore(backing)

  // Queue the fresh upsert FIRST, then the reconcile remove of the old nonce.
  const upsertP = store.upsert({ id: 'profile:a', ownerNonce: 'new', lifecycle: 'ready' })
  const commitP = store.commitReconcile({ removeTargets: [{ id: 'profile:a', ownerNonce: 'old' }] })
  await Promise.all([upsertP, commitP])
  await store.drain()

  const { removedIds } = await commitP
  assert.deepEqual(removedIds, [])
  assert.equal(backing.state.records.length, 1)
  assert.equal(backing.state.records[0].ownerNonce, 'new')
})
