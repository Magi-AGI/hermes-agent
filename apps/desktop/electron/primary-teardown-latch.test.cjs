const test = require('node:test')
const assert = require('node:assert/strict')
const { createPrimaryTeardownLatch, childHasExited } = require('./primary-teardown-latch.cjs')

// A minimal ChildProcess stand-in. Alive until exit() flips exitCode, matching
// how Node populates exitCode/signalCode on a real handle when the OS reaps it.
function fakeChild(pid = 1234) {
  const listeners = []
  return {
    pid,
    exitCode: null,
    signalCode: null,
    once(event, fn) {
      if (event === 'exit') listeners.push(fn)
    },
    exit(code = 0) {
      this.exitCode = code
      listeners.splice(0).forEach(fn => fn(code, null))
    }
  }
}

test('childHasExited: null child is treated as gone; live child is not', () => {
  assert.equal(childHasExited(null), true)
  assert.equal(childHasExited({ exitCode: null, signalCode: null }), false)
  assert.equal(childHasExited({ exitCode: 0, signalCode: null }), true)
  assert.equal(childHasExited({ exitCode: null, signalCode: 'SIGKILL' }), true)
})

test('latch is inactive until armed', () => {
  const latch = createPrimaryTeardownLatch()
  assert.equal(latch.isActive(), false)
  assert.equal(latch.snapshot(), null)
  assert.equal(latch.child(), null)
})

test('armed latch stays active while its child is alive, self-clears on exit', () => {
  const latch = createPrimaryTeardownLatch()
  const child = fakeChild(42)
  latch.arm({ child, pid: child.pid, since: 1000, reason: 'teardown-unconfirmed' })

  assert.equal(latch.isActive(), true)
  assert.equal(latch.child(), child)
  assert.equal(latch.reason(), 'teardown-unconfirmed')
  assert.deepEqual(latch.snapshot(), { child, pid: 42, since: 1000, reason: 'teardown-unconfirmed' })

  // The child dies (OS reaps it); the very next consult self-clears.
  child.exit(0)
  assert.equal(latch.isActive(), false)
  assert.equal(latch.snapshot(), null)
  assert.equal(latch.child(), null)
})

// The core Codex scenario: a *repeated* unconfirmed primary teardown must keep
// the primary blocked, not treat the nulled handle as success on the 2nd call.
// This simulator mirrors main.cjs's teardownPrimaryBackendAndWait + startHermes
// gate using the REAL latch, so the behavior — not just a regex — is proven.
function makePrimarySim() {
  const latch = createPrimaryTeardownLatch()
  let hermesProcess = null // the live handle, nulled on teardown like main.cjs
  let phase = 'ready'

  return {
    latch,
    setLiveChild(child) {
      hermesProcess = child
    },
    phase: () => (latch.isActive() ? 'failed' : phase),
    // Mirrors teardownPrimaryBackendAndWait(): pick the dying target (live handle,
    // else the latched child), null the handle, then arm/clear on the confirm.
    async teardown(confirmedExit) {
      let dying = hermesProcess || null
      if (!dying && latch.isActive()) dying = latch.child()
      hermesProcess = null
      const confirmed = dying ? confirmedExit(dying) : true
      if (confirmed) {
        latch.clear()
        phase = 'missing'
      } else {
        latch.arm({ child: dying, pid: dying ? dying.pid : null, since: 0 })
        phase = 'failed'
        // Mirror watchLatchedPrimaryChildExit(): a late exit clears the latch and
        // drops the blocked phase so status returns to 'missing'.
        if (dying && typeof dying.once === 'function') {
          dying.once('exit', () => {
            if (latch.child() === dying) {
              latch.clear()
              if (phase === 'failed') phase = 'missing'
            }
          })
        }
      }
      return confirmed
    },
    // Mirrors startHermes()'s gate: refuse while the latch is active.
    startAllowed() {
      return !latch.isActive()
    }
  }
}

test('repeated unconfirmed primary teardown keeps the primary blocked across calls', () => {
  const sim = makePrimarySim()
  const child = fakeChild(7)
  sim.setLiveChild(child)

  // 1st teardown: child refuses to confirm exit → unconfirmed, blocked.
  const first = sim.teardown(() => false)
  return first.then(confirmed1 => {
    assert.equal(confirmed1, false)
    assert.equal(sim.startAllowed(), false, 'start must be blocked after 1st unconfirmed teardown')
    assert.equal(sim.phase(), 'failed', 'status must be failed, never missing')

    // 2nd teardown: the live handle is already null, but the latched child is
    // still alive — it must be re-waited, NOT reported as success.
    return sim.teardown(dying => {
      assert.equal(dying, child, 're-teardown must target the LATCHED child, not null')
      return false
    }).then(confirmed2 => {
      assert.equal(confirmed2, false, 'still unconfirmed while the wedged child lives')
      assert.equal(sim.startAllowed(), false, 'start still blocked on the 2nd call')
      assert.equal(sim.phase(), 'failed')

      // The child finally dies; latch self-clears, start is allowed, status missing.
      child.exit(0)
      assert.equal(sim.startAllowed(), true, 'start allowed once the child is confirmed gone')
      assert.equal(sim.phase(), 'missing')
    })
  })
})

test('a confirmed primary teardown never arms the latch (start stays allowed)', () => {
  const sim = makePrimarySim()
  const child = fakeChild(9)
  sim.setLiveChild(child)
  return sim.teardown(() => true).then(confirmed => {
    assert.equal(confirmed, true)
    assert.equal(sim.startAllowed(), true)
    assert.equal(sim.phase(), 'missing')
    assert.equal(sim.latch.isActive(), false)
  })
})

test('remote primary (no child) teardown is always confirmed and never latches', () => {
  const sim = makePrimarySim()
  // No live child set — mirrors a remote primary. dying resolves to null → true.
  return sim.teardown(() => false).then(confirmed => {
    assert.equal(confirmed, true, 'no child → confirmed exit')
    assert.equal(sim.startAllowed(), true)
    assert.equal(sim.latch.isActive(), false)
  })
})
