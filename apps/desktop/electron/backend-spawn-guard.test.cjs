const test = require('node:test')
const assert = require('node:assert/strict')

const { reserveThenSpawn } = require('./backend-spawn-guard.cjs')

test('reservation conflict throws via onConflict and NEVER calls spawnFn', async () => {
  let spawnCalls = 0
  await assert.rejects(
    () =>
      reserveThenSpawn({
        reserve: async () => ({ upserted: false, conflict: true, id: 'profile:a' }),
        spawnFn: async () => {
          spawnCalls += 1
          return 'spawned'
        },
        onConflict: () => {
          throw new Error('a previous unresponsive generation is still being reaped')
        }
      }),
    /still being reaped/
  )
  assert.equal(spawnCalls, 0, 'spawnFn must not run when the reservation conflicts')
})

test('explicit { upserted: true } reservation runs spawnFn and returns its result', async () => {
  const order = []
  const result = await reserveThenSpawn({
    reserve: async () => {
      order.push('reserve')
      return { upserted: true, id: 'profile:a' }
    },
    spawnFn: async () => {
      order.push('spawn')
      return 'backend'
    },
    onConflict: () => {
      throw new Error('should not be called')
    }
  })

  assert.equal(result, 'backend')
  assert.deepEqual(order, ['reserve', 'spawn'], 'reserve strictly precedes spawn')
})

test('undefined/null reservation FAILS CLOSED (no spawn) — not treated as success', async () => {
  for (const bad of [undefined, null]) {
    let spawnCalls = 0
    await assert.rejects(
      () =>
        reserveThenSpawn({
          reserve: async () => bad,
          spawnFn: async () => {
            spawnCalls += 1
          },
          onConflict: () => {
            throw new Error('should not be called')
          }
        }),
      /did not durably succeed/
    )
    assert.equal(spawnCalls, 0)
  }
})

test('a non-success reservation result (write failure) fails closed via onReservationFailed', async () => {
  let spawnCalls = 0
  let failedArg = null
  const result = await reserveThenSpawn({
    reserve: async () => ({ upserted: false, error: true, id: 'profile:a' }),
    spawnFn: async () => {
      spawnCalls += 1
    },
    onConflict: () => {
      throw new Error('should not be called')
    },
    onReservationFailed: reservation => {
      failedArg = reservation
      return 'handled'
    }
  })

  assert.equal(result, 'handled')
  assert.equal(spawnCalls, 0, 'spawnFn must not run when the reservation write failed')
  assert.deepEqual(failedArg, { upserted: false, error: true, id: 'profile:a' })
})

test('a rejecting reserve() propagates and never calls spawnFn', async () => {
  let spawnCalls = 0
  await assert.rejects(
    () =>
      reserveThenSpawn({
        reserve: async () => {
          throw new Error('registry write exploded')
        },
        spawnFn: async () => {
          spawnCalls += 1
        },
        onConflict: () => {
          throw new Error('should not be called')
        }
      }),
    /registry write exploded/
  )
  assert.equal(spawnCalls, 0)
})
