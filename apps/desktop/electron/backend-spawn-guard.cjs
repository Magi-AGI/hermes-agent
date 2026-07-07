'use strict'

// Reserve a backend's registry generation BEFORE launching any process (R11),
// FAIL CLOSED (R13).
//
// The reaper's store refuses to overwrite a tombstoned prior generation (an
// un-reaped wedged backend) that still holds this id. If we spawned the child
// first and only THEN discovered the conflict, an unconfirmed kill of that child
// would leave a recordless survivor. Reserving first means no replacement
// process is ever started unless the reservation DURABLY succeeded.
//
// spawnFn runs ONLY on an explicit success (`reservation.upserted === true`):
//   - conflict ({conflict:true})            → onConflict() (throw retry/reap)
//   - write failure / non-success / null    → onReservationFailed() (fail closed)
//   - reserve() rejects                     → the rejection propagates (no spawn)
// Treating undefined/failure as success would fail OPEN — a child with no
// durable registry row — which is exactly the recordless-survivor risk M4's
// 10-session fanout would amplify.
//
// Pure/injectable so the ordering + fail-closed contract is unit-testable
// without Electron.
async function reserveThenSpawn({ reserve, spawnFn, onConflict, onReservationFailed }) {
  const reservation = await reserve()
  if (reservation && reservation.conflict) {
    return onConflict(reservation)
  }
  if (!reservation || reservation.upserted !== true) {
    // Reservation did not durably succeed — never spawn without a durable row.
    if (onReservationFailed) return onReservationFailed(reservation)
    throw new Error('backend reservation did not durably succeed; not spawning')
  }
  return spawnFn(reservation)
}

module.exports = { reserveThenSpawn }
