const test = require('node:test')
const assert = require('node:assert/strict')

const { confirmProcessKilled } = require('./backend-kill.cjs')

test('confirms only when the liveness probe reports the process gone', () => {
  const killed = []
  const ok = confirmProcessKilled(123, {
    kill: pid => killed.push(pid),
    isAlive: () => false // gone
  })
  assert.equal(ok, true)
  assert.deepEqual(killed, [123])
})

test('returns false when the process still appears alive after the kill', () => {
  const ok = confirmProcessKilled(123, {
    kill: () => {},
    isAlive: () => true
  })
  assert.equal(ok, false)
})

test('fails toward false when the liveness probe is ambiguous (throws)', () => {
  const ok = confirmProcessKilled(123, {
    kill: () => {},
    isAlive: () => {
      throw new Error('access denied')
    }
  })
  assert.equal(ok, false)
})

test('undefined liveness result is NOT treated as confirmed', () => {
  const ok = confirmProcessKilled(123, {
    kill: () => {},
    isAlive: () => undefined
  })
  assert.equal(ok, false)
})

test('a throwing kill is best-effort: still confirmed if the probe shows gone', () => {
  const ok = confirmProcessKilled(123, {
    kill: () => {
      throw new Error('taskkill failed')
    },
    isAlive: () => false
  })
  assert.equal(ok, true)
})

test('a throwing kill with a still-alive probe is not confirmed', () => {
  const ok = confirmProcessKilled(123, {
    kill: () => {
      throw new Error('taskkill failed')
    },
    isAlive: () => true
  })
  assert.equal(ok, false)
})

test('non-positive/invalid pid is a no-op that reports gone and never kills', () => {
  let killCalls = 0
  const kill = () => {
    killCalls += 1
  }
  assert.equal(confirmProcessKilled(0, { kill, isAlive: () => true }), true)
  assert.equal(confirmProcessKilled(-5, { kill, isAlive: () => true }), true)
  assert.equal(confirmProcessKilled(undefined, { kill, isAlive: () => true }), true)
  assert.equal(killCalls, 0)
})
