import assert from 'node:assert/strict'

import { test } from 'vitest'

import { createReopenQueue, type ReopenHalt, type ReopenStep } from './reopen-queue'

const e = (windowId: string) => ({ windowId })

// A queue whose runEntry blocks until the test releases that window, so the
// test can observe how many windows are being opened at the same moment.
function harness(verdicts: Record<string, ReopenStep> = {}) {
  const opening = new Set<string>()
  const log: string[] = []
  const halts: ReopenHalt[] = []
  const waiters = new Map<string, () => void>()
  let maxConcurrent = 0

  const queue = createReopenQueue<{ windowId: string }>({
    runEntry: async entry => {
      opening.add(entry.windowId)
      maxConcurrent = Math.max(maxConcurrent, opening.size)
      log.push(entry.windowId)
      await new Promise<void>(resolve => waiters.set(entry.windowId, resolve))
      opening.delete(entry.windowId)

      // A failure verdict applies once: the retried window then comes up.
      const verdict = verdicts[entry.windowId] ?? { ok: true }

      delete verdicts[entry.windowId]

      return verdict
    },
    onHalt: halt => halts.push(halt)
  })

  const tick = () => new Promise(resolve => setTimeout(resolve, 0))

  return {
    queue,
    log,
    halts,
    maxConcurrent: () => maxConcurrent,
    // Releases whichever window is currently opening; waits for the next to start.
    async release(windowId: string) {
      await tick()
      const resolve = waiters.get(windowId)

      assert.ok(resolve, `${windowId} is not opening (log: ${log.join(',')})`)
      waiters.delete(windowId)
      resolve()
      await tick()
    }
  }
}

test('Reopen and Resume never open windows in parallel: a Reopen during a Resume merges into it', async () => {
  const h = harness({ b: { ok: false, profile: 'p', reason: 'unready' } })

  // First run halts at b, leaving [b, c] for Resume.
  const first = h.queue.reopen([e('a'), e('b'), e('c')])

  await h.release('a')
  await h.release('b')
  await first
  assert.deepEqual(h.queue.haltedIds, ['b', 'c'])

  // Resume starts; a Reopen issued while it runs must merge into it, not open
  // a second queue's windows alongside it.
  const resume = h.queue.resume()
  const reopen = h.queue.reopen([e('c'), e('x')])

  await h.release('b')
  await h.release('c')
  await h.release('x')
  await Promise.all([reopen, resume])

  assert.equal(h.maxConcurrent(), 1)
  assert.deepEqual(h.log, ['a', 'b', 'b', 'c', 'x'])
})

test('Resume while a resumed queue runs joins it (one run)', async () => {
  const h = harness({ a: { ok: false, profile: null, reason: 'unready' } })

  const first = h.queue.reopen([e('a'), e('b')])

  await h.release('a')
  await first

  h.log.length = 0
  const r1 = h.queue.resume()
  const r2 = h.queue.resume()

  await h.release('a')
  await h.release('b')
  assert.deepEqual(await r1, { ok: true })
  assert.deepEqual(await r2, { ok: true })
  assert.equal(h.maxConcurrent(), 1)
  assert.deepEqual(h.log, ['a', 'b'])
})

test('a Reopen during an active queue merges new windows into it (no duplicates)', async () => {
  const h = harness()

  const first = h.queue.reopen([e('a'), e('b')])
  const second = h.queue.reopen([e('b'), e('c')])

  await h.release('a')
  await h.release('b')
  await h.release('c')
  await Promise.all([first, second])

  assert.equal(h.maxConcurrent(), 1)
  assert.deepEqual(h.log, ['a', 'b', 'c'])
})

test('a halt keeps the failed window and everything after it for Resume', async () => {
  const h = harness({ b: { ok: false, profile: 'p', reason: 'timeout' } })

  const run = h.queue.reopen([e('a'), e('b'), e('c')])

  await h.release('a')
  await h.release('b')
  await run

  assert.deepEqual(h.queue.haltedIds, ['b', 'c'])
  assert.deepEqual(h.halts, [{ restoredCount: 1, totalCount: 3, profile: 'p', reason: 'timeout' }])
})

test('Resume with nothing halted reports it', async () => {
  const h = harness()

  assert.deepEqual(await h.queue.resume(), { ok: false, error: 'nothing to resume' })
})

test('a fresh Reopen discards a halted queue', async () => {
  const h = harness({ a: { ok: false, profile: null, reason: 'unready' } })

  const first = h.queue.reopen([e('a')])

  await h.release('a')
  await first
  assert.deepEqual(h.queue.haltedIds, ['a'])

  const second = h.queue.reopen([e('z')])

  assert.deepEqual(h.queue.haltedIds, [])
  await h.release('z')
  await second
})
