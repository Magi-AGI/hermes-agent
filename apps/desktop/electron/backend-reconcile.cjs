'use strict'

// Orchestrates the Desktop backend registry reaper. The fragile pieces (OS
// process enumeration, tree-kill, Electron globals) are injected so this flow
// is fully unit-testable with fakes/spies — main.cjs supplies the real deps.
// Decision logic lives in backend-registry.cjs (reconcileBackendRegistry).

const { buildProcessTableFromRows, reconcileBackendRegistry } = require('./backend-registry.cjs')

/**
 * @param {object} deps
 * @param {() => {records: any[]}} deps.readRegistry
 * @param {(decisions: {removeTargets: any[], tombstoneTargets: any[]}) => Promise<{removedIds: string[], tombstonedIds: string[]}>} deps.commitReconcile  generation-safe apply
 * @param {() => any[]} deps.enumerateProcessRows  throws on failure
 * @param {(pid: number) => boolean} deps.killBackendTree  returns whether the tree is confirmed gone
 * @param {() => Set<string>|string[]} deps.collectClaimedIds
 * @param {(records: any[]) => object} deps.loadSidecars
 * @param {(sidecarPath: string) => void} deps.removeSidecar
 * @param {string} deps.expectedInstallRoot
 * @param {string} deps.currentInstanceId
 * @param {() => number} [deps.now]
 * @param {(message: string) => void} [deps.log]
 */
function createBackendReconciler(deps) {
  const log = deps.log || (() => undefined)
  const now = deps.now || (() => Date.now())

  // Kill a pid (+ any children) and report whether the whole tree is confirmed
  // gone. Requires an EXPLICIT `true` from killBackendTree — undefined, false, a
  // non-true value, or a thrown error all mean "not confirmed" → the caller
  // tombstones instead of deleting, so a survivor stays findable next pass.
  // (The production adapter returns undefined for best-effort kills, so treating
  // undefined as success would silently drop records for surviving processes.)
  function killTreeConfirmed(pid, children) {
    let confirmed = false
    try {
      confirmed = deps.killBackendTree(pid) === true
    } catch (error) {
      log(`[reconcile] kill pid=${pid} failed: ${error.message}`)
      confirmed = false
    }
    for (const child of children || []) {
      try {
        if (deps.killBackendTree(child) !== true) confirmed = false
      } catch {
        confirmed = false
      }
    }
    return confirmed
  }

  async function reconcile(reason) {
    let records
    try {
      records = deps.readRegistry().records
    } catch (error) {
      log(`[reconcile:${reason}] could not read backend registry: ${error.message}`)
      return null
    }
    if (!Array.isArray(records) || records.length === 0) return { reaped: 0, pruned: 0, tombstoned: 0, kept: 0 }

    let rows
    try {
      rows = deps.enumerateProcessRows()
    } catch (error) {
      // Fail closed on enumeration errors: never treat every backend as dead
      // (that would prune live rows and lose track of real orphans).
      log(`[reconcile:${reason}] process enumeration failed; skipping reconciliation: ${error.message}`)
      return null
    }

    const processTable = buildProcessTableFromRows(rows, { expectedInstallRoot: deps.expectedInstallRoot })
    const actions = reconcileBackendRegistry(records, processTable, {
      expectedInstallRoot: deps.expectedInstallRoot,
      currentInstanceId: deps.currentInstanceId,
      claimedIds: deps.collectClaimedIds(),
      sidecars: deps.loadSidecars(records)
    })

    const recordById = new Map(records.map(record => [record.id, record]))
    // Build (id, ownerNonce)-tagged decisions so the generation-safe commit only
    // touches the exact rows the reaper judged — a fresh same-id spawn (new
    // nonce) written while we were deciding is preserved, never clobbered.
    const removeTargets = []     // {id, ownerNonce, kind: 'reaped' | 'pruned'}
    const tombstoneTargets = []  // {id, ownerNonce, patch}
    for (const action of actions) {
      const record = recordById.get(action.id)
      const ownerNonce = record ? record.ownerNonce || null : null
      if (action.action === 'kill_tree') {
        log(`[reconcile:${reason}] reaping backend ${action.id} pid=${action.pid} (${action.reason})`)
        if (killTreeConfirmed(action.pid, action.children)) {
          removeTargets.push({ id: action.id, ownerNonce, kind: 'reaped' })
        } else {
          // Kill unconfirmed: keep the record + sidecar but tombstone the
          // lifecycle so the NEXT reconcile still finds and reaps the survivor.
          tombstoneTargets.push({
            id: action.id,
            ownerNonce,
            patch: { lifecycle: 'failed', tombstonedAt: now(), tombstoneReason: action.reason }
          })
          log(`[reconcile:${reason}] backend ${action.id} pid=${action.pid} did not confirm exit; tombstoned for retry`)
        }
      } else if (action.action === 'prune') {
        removeTargets.push({ id: action.id, ownerNonce, kind: 'pruned' })
      }
    }

    let reaped = 0
    let pruned = 0
    let tombstoned = 0
    if (removeTargets.length || tombstoneTargets.length) {
      let committed = { removedIds: [], tombstonedIds: [] }
      try {
        committed = await deps.commitReconcile({ removeTargets, tombstoneTargets })
      } catch (error) {
        log(`[reconcile:${reason}] failed to commit registry changes: ${error.message}`)
      }
      const removedIds = new Set(committed.removedIds || [])
      const kindById = new Map(removeTargets.map(target => [target.id, target.kind]))
      for (const id of removedIds) {
        if (kindById.get(id) === 'pruned') pruned += 1
        else reaped += 1
      }
      tombstoned = (committed.tombstonedIds || []).length
      // Unlink sidecars ONLY for rows actually removed (nonce still matched);
      // tombstoned survivors and superseded ids keep their sidecar.
      for (const id of removedIds) {
        const record = recordById.get(id)
        if (record && record.sidecarPath) deps.removeSidecar(record.sidecarPath)
      }
    }

    return { reaped, pruned, tombstoned, kept: records.length - reaped - pruned - tombstoned }
  }

  return { reconcile }
}

module.exports = { createBackendReconciler }
