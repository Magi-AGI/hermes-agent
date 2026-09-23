import assert from 'node:assert/strict'

import { test } from 'vitest'

import { createReopenQueue, type ReopenHalt, type ReopenStep } from './reopen-queue'

const e = (windowId: string) => ({ windowId })

// A queue whose runEntry blocks until the test releases that window, so the
// test can observe how many windows are being opened at the same moment.
function harness(verdicts: Record<string, ReopenStep> = {}) {
  const forgotten = new Set<string>()
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
    onHalt: halt => halts.push(halt),
    isForgotten: id => forgotten.has(id)
  })

  const tick = () => new Promise(resolve => setTimeout(resolve, 0))

  return {
    queue,
    forgotten,
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

test('a Reopen never re-admits a window the active queue is checking or already opened (Codex A4 r4 #3)', async () => {
  const h = harness()

  const first = h.queue.reopen([e('a'), e('b')])

  // a is being checked: a Reopen of [a, b, c] must add only c.
  const second = h.queue.reopen([e('a'), e('b'), e('c')])

  await h.release('a')
  await h.release('b')

  // b is done now too: re-admitting it would reopen it again.
  const third = h.queue.reopen([e('b'), e('c')])

  await h.release('c')
  await Promise.all([first, second, third])

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

// ─── Codex A4 round 3 #2: Forget invalidates captured entries ────────────────

test('a window forgotten while an active queue runs is never opened', async () => {
  const h = harness()

  const run = h.queue.reopen([e('a'), e('b'), e('c')])

  // a is opening; the user forgets b (its entry was already captured).
  h.forgotten.add('b')
  h.queue.forget('b')
  await h.release('a')
  await h.release('c')
  await run

  assert.deepEqual(h.log, ['a', 'c'])
})

test('each entry re-checks Forget immediately before opening', async () => {
  const h = harness()

  const run = h.queue.reopen([e('a'), e('b')])

  // Only the forgotten predicate changes (no queue.forget call): the captured
  // entry must still not be opened.
  h.forgotten.add('b')
  await h.release('a')
  await run

  assert.deepEqual(h.log, ['a'])
})

test('a window forgotten from a halted queue is not reopened by Resume', async () => {
  const h = harness({ b: { ok: false, profile: null, reason: 'unready' } })

  const first = h.queue.reopen([e('a'), e('b'), e('c')])

  await h.release('a')
  await h.release('b')
  await first

  h.forgotten.add('c')
  h.queue.forget('c')
  assert.deepEqual(h.queue.haltedIds, ['b'])

  const resume = h.queue.resume()

  await h.release('b')
  await resume
  assert.deepEqual(h.log, ['a', 'b', 'b'])
})

test('forgetting the window being verified does not halt the queue on it', async () => {
  const h = harness({ a: { ok: false, profile: null, reason: 'window closed' } })

  const run = h.queue.reopen([e('a'), e('b')])

  h.forgotten.add('a')
  h.queue.forget('a')
  await h.release('a')
  await h.release('b')
  await run

  assert.deepEqual(h.halts, [])
  assert.deepEqual(h.log, ['a', 'b'])
})

test('a Reopen never enqueues a forgotten window', async () => {
  const h = harness()

  h.forgotten.add('a')

  const run = h.queue.reopen([e('a'), e('b')])

  await h.release('b')
  await run
  assert.deepEqual(h.log, ['b'])
})
