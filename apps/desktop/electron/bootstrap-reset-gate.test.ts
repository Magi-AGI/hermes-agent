import assert from 'node:assert/strict'

import { test } from 'vitest'

import { createBootstrapResetGate } from './bootstrap-reset-gate'

function deferred() {
  let resolve!: () => void
  const promise = new Promise<void>(r => (resolve = r))

  return { promise, resolve }
}

test('retries clicked together in several windows tear the backend down once', async () => {
  const release = deferred()
  let teardowns = 0

  const gate = createBootstrapResetGate(async () => {
    teardowns += 1
    await release.promise
  })

  const failedEpoch = gate.epoch()
  const clicks = [gate.request(failedEpoch), gate.request(failedEpoch), gate.request(failedEpoch)]
  release.resolve()
  const outcomes = await Promise.all(clicks)

  assert.equal(teardowns, 1)
  assert.deepEqual(
    outcomes.map(o => o.performed),
    [true, false, false]
  )
})

test('a retry of a failure that a completed reset already superseded does not tear down the new boot', async () => {
  let teardowns = 0
  const gate = createBootstrapResetGate(async () => void (teardowns += 1))

  const staleEpoch = gate.epoch()
  await gate.request(staleEpoch)
  // Another window, whose document loaded before that reset, clicks Retry seconds later.
  const late = await gate.request(staleEpoch)

  assert.equal(teardowns, 1)
  assert.equal(late.performed, false)
})

test('a document loaded after a reset can reset again', async () => {
  let teardowns = 0
  const gate = createBootstrapResetGate(async () => void (teardowns += 1))

  await gate.request(gate.epoch())
  const reloadedEpoch = gate.epoch()
  const retry = await gate.request(reloadedEpoch)

  assert.equal(teardowns, 2)
  assert.equal(retry.performed, true)
})

test('a document that loaded while the teardown ran does not trigger a second reset', async () => {
  const release = deferred()
  let teardowns = 0

  const gate = createBootstrapResetGate(async () => {
    teardowns += 1
    await release.promise
  })

  const first = gate.request(gate.epoch())
  const loadedDuringTeardown = gate.epoch()
  release.resolve()
  await first

  assert.equal((await gate.request(loadedDuringTeardown)).performed, false)
  assert.equal(teardowns, 1)
})

test('callers without an epoch still reset, but join one in flight', async () => {
  const release = deferred()
  let teardowns = 0

  const gate = createBootstrapResetGate(async () => {
    teardowns += 1
    await release.promise
  })

  const both = [gate.request(), gate.request()]
  release.resolve()
  await Promise.all(both)
  await gate.request()

  assert.equal(teardowns, 2)
})

test('a failed teardown still releases the gate and advances the epoch', async () => {
  let calls = 0

  const gate = createBootstrapResetGate(async () => {
    calls += 1

    if (calls === 1) {
      throw new Error('teardown failed')
    }
  })

  await assert.rejects(gate.request(0))
  assert.equal(gate.epoch(), 1)
  assert.equal((await gate.request(1)).performed, true)
})
