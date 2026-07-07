'use strict'

// Serialized, generation-safe registry mutation store (R9a).
//
// MVP assumes ONE Electron base instance per HERMES_HOME (cross-process locking
// is deferred to R9b), but that one process spawns/reaps up to ~10 session
// backends concurrently. The real intra-process hazard is a lost update: the
// async reaper reads a registry snapshot, awaits process enumeration, then
// rewrites the whole file — meanwhile a same-id spawn upserts a FRESH row, which
// the stale-snapshot rewrite would clobber.
//
// This store closes that by (a) serializing every read-modify-write through a
// promise chain so no two mutations interleave, and (b) having each mutation
// RE-READ the current records under the lock and match rows by (id, ownerNonce)
// so it only ever touches the exact generation it intended — a fresh same-id row
// written with a new nonce is always preserved.
//
// Injected readRecords()/writeRecords(records) do the actual (sync) file IO.
function createRegistryStore({ readRecords, writeRecords }) {
  let tail = Promise.resolve()

  // Chain fn after all prior mutations settle (success or failure), so the
  // read-modify-write below is atomic w.r.t. every other store mutation.
  function runExclusive(fn) {
    const run = tail.then(() => fn())
    tail = run.then(() => undefined, () => undefined)
    return run
  }

  function nonceMatches(record, ownerNonce) {
    // A null/absent expected nonce matches anything (legacy rows); otherwise the
    // row must carry no nonce or the SAME nonce. A different nonce = a newer
    // generation reused the id → never touch it.
    return !ownerNonce || !record.ownerNonce || record.ownerNonce === ownerNonce
  }

  function upsert(record) {
    return runExclusive(() => {
      const records = readRecords()
      const existing = records.find(row => row && row.id === record.id)
      // R10 guard: never clobber a TOMBSTONED prior generation (lifecycle
      // 'failed') that carries a DIFFERENT nonce — it may still reference a live
      // old process the reaper hasn't cleared. The caller must not spawn a
      // same-id replacement until that generation is reaped; report the conflict
      // so it can fail clearly/retry instead of orphaning the old row.
      if (
        existing &&
        existing.lifecycle === 'failed' &&
        existing.ownerNonce &&
        record.ownerNonce &&
        existing.ownerNonce !== record.ownerNonce
      ) {
        return { upserted: false, conflict: true, id: record.id }
      }
      const kept = records.filter(row => !(row && row.id === record.id))
      kept.push(record)
      writeRecords(kept)
      return { upserted: true, id: record.id }
    })
  }

  function remove(id, { ownerNonce = null } = {}) {
    return runExclusive(() => {
      const records = readRecords()
      const match = records.find(existing => existing && existing.id === id)
      if (!match) return { removed: false }
      if (!nonceMatches(match, ownerNonce)) return { removed: false } // fresh row, leave it
      writeRecords(records.filter(existing => !(existing && existing.id === id)))
      return { removed: true }
    })
  }

  function tombstone(id, { ownerNonce = null, patch = {} } = {}) {
    return runExclusive(() => {
      const records = readRecords()
      let found = false
      const updated = records.map(existing => {
        if (existing && existing.id === id && nonceMatches(existing, ownerNonce)) {
          found = true
          return { ...existing, ...patch }
        }
        return existing
      })
      if (found) writeRecords(updated)
      return { tombstoned: found }
    })
  }

  // Apply a batch of reaper decisions generation-safely. removeTargets and
  // tombstoneTargets each carry {id, ownerNonce}; the write RE-READS current
  // records so any row whose nonce changed since the reaper's snapshot (a fresh
  // same-id spawn) is left untouched. Returns which ids were actually removed
  // (so the caller only unlinks sidecars for rows it truly deleted).
  function commitReconcile({ removeTargets = [], tombstoneTargets = [] } = {}) {
    return runExclusive(() => {
      const removeById = new Map(removeTargets.map(target => [target.id, target]))
      const tombstoneById = new Map(tombstoneTargets.map(target => [target.id, target]))
      const current = readRecords()
      const kept = []
      const removedIds = []
      const tombstonedIds = []
      for (const record of current) {
        if (!record) continue
        const removeTarget = removeById.get(record.id)
        if (removeTarget && nonceMatches(record, removeTarget.ownerNonce)) {
          removedIds.push(record.id)
          continue
        }
        const tombstoneTarget = tombstoneById.get(record.id)
        if (tombstoneTarget && nonceMatches(record, tombstoneTarget.ownerNonce)) {
          kept.push({ ...record, ...(tombstoneTarget.patch || {}) })
          tombstonedIds.push(record.id)
          continue
        }
        kept.push(record)
      }
      if (removedIds.length || tombstonedIds.length) writeRecords(kept)
      return { removedIds, tombstonedIds }
    })
  }

  return {
    upsert,
    remove,
    tombstone,
    commitReconcile,
    runExclusive,
    // Test seam: resolves once the current mutation chain has drained.
    drain: () => tail
  }
}

module.exports = { createRegistryStore }
