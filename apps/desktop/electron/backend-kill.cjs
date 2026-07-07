'use strict'

// Confirmed process termination. The reaper must only DROP a registry row +
// sidecar when the backend is genuinely gone; if a kill is best-effort or fails
// silently (Windows taskkill swallows errors), it must tombstone instead so the
// survivor stays findable. This primitive makes "confirmed gone" explicit and
// fails toward NOT-confirmed whenever liveness is ambiguous.
//
// Pure and injectable: `kill(pid)` performs the OS kill (best-effort, may throw
// or no-op), `isAlive(pid)` returns true (alive) / false (gone) / anything else
// or throws (ambiguous → treated as alive). Returns true ONLY when the process
// is confirmed gone.
function confirmProcessKilled(pid, { kill, isAlive }) {
  if (!Number.isInteger(pid) || pid <= 0) return true // nothing to kill → "gone"
  try {
    kill(pid)
  } catch {
    // Best effort — the liveness probe below is the real gate.
  }
  let alive
  try {
    alive = isAlive(pid)
  } catch {
    // Ambiguous probe → assume still alive → not confirmed.
    alive = true
  }
  // Only an explicit "gone" (false) confirms termination; undefined/true/other
  // all mean not-confirmed so the caller tombstones rather than deletes.
  return alive === false
}

module.exports = { confirmProcessKilled }
