'use strict'

// Primary (window) backend "unconfirmed teardown" latch.
//
// When teardownPrimaryBackendAndWait() cannot confirm the local primary child
// actually exited (SIGKILL/taskkill issued but the handle never reported exit
// within the timeout), we must NOT let a later startHermes()/getConnection()
// spawn a replacement — the wedged child may still hold the local port and the
// backend registry ownership. The live process handle is nulled as part of
// teardown, so "hermesProcess === null" is NOT a safe proxy for "gone". This
// latch remembers the specific child so subsequent calls re-check THAT process
// instead of treating the missing handle as success.
//
// The latch self-heals: isActive() re-reads the latched child's exit state on
// every consult, so once the OS finally reaps the process the latch clears with
// no explicit event required (Node populates exitCode/signalCode on the handle
// independently of user 'exit' listeners). A one-shot exit listener in the host
// clears it eagerly too, but correctness does not depend on that listener.
//
// Pure and side-effect free apart from the closed-over latch cell, so it is
// unit-tested directly without an Electron/child-process harness.

// Default liveness predicate for a Node ChildProcess: exitCode and signalCode
// are both null while alive and one becomes non-null exactly once it exits. A
// null/absent child is treated as already-exited (nothing to wait on).
function childHasExited(child) {
  if (!child) {
    return true
  }
  return child.exitCode !== null || child.signalCode !== null
}

function createPrimaryTeardownLatch(options = {}) {
  const isExited = typeof options.isExited === 'function' ? options.isExited : childHasExited
  // { child, pid, since, reason } | null
  let latch = null

  return {
    // Arm after an UNCONFIRMED local-primary teardown. Callers must never arm
    // for a remote primary (no child) — a null child would be reported exited
    // immediately, but arming it is still meaningless, so guard at the call site.
    arm({ child = null, pid = null, since = 0, reason = 'teardown-unconfirmed' } = {}) {
      latch = { child, pid, since, reason }
      return { ...latch }
    },

    // True while the latch is armed AND the latched child still has not exited.
    // Self-clears (and returns false) the moment the child is observed exited.
    isActive() {
      if (!latch) {
        return false
      }
      if (isExited(latch.child)) {
        latch = null
        return false
      }
      return true
    },

    // Non-mutating peek for status/tests. Returns a copy or null.
    snapshot() {
      return latch ? { ...latch } : null
    },

    // The armed child handle (or null). Used by teardown to re-target its wait.
    child() {
      return latch ? latch.child : null
    },

    reason() {
      return latch ? latch.reason : null
    },

    // Force-clear — used by the host's one-shot exit listener when the latched
    // child finally reports exit outside of a teardown retry.
    clear() {
      latch = null
    }
  }
}

module.exports = { createPrimaryTeardownLatch, childHasExited }
