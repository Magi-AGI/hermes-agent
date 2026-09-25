// Single-flight gate for the renderer's "Reload and retry" (hermes:bootstrap:reset).
//
// Every session window shares ONE primary backend, and the boot-failure overlay's Retry tears it
// down. Retries clicked in several windows used to race: each teardown killed the backend the
// previous retry had just started, a self-sustaining storm (5+ cycles, ~13 s apart, 2026-08-27).
// A Retry now carries the reset epoch its document started under (main tracks it per window). A
// reset already in flight is joined, and a Retry from a document older than a completed reset is
// superseded: that reset already started the boot the window reloads into. Callers that send no
// epoch (install retry, cloud sign-in) still reset, but join an in-flight one instead of a second.

export interface BootstrapResetOutcome {
  ok: true
  /** False when this call joined an in-flight reset or was superseded by a completed one. */
  performed: boolean
}

export interface BootstrapResetGate {
  /** Resets completed so far; main records it for each window document at registration. */
  epoch: () => number
  request: (documentEpoch?: unknown) => Promise<BootstrapResetOutcome>
}

export function createBootstrapResetGate(performReset: () => Promise<void>): BootstrapResetGate {
  let epoch = 0
  let inFlight: Promise<void> | null = null

  const request = async (documentEpoch?: unknown): Promise<BootstrapResetOutcome> => {
    if (inFlight) {
      await inFlight

      return { ok: true, performed: false }
    }

    if (typeof documentEpoch === 'number' && documentEpoch < epoch) {
      return { ok: true, performed: false }
    }

    // The epoch advances when the reset SETTLES (even a failed one tore something down), so a
    // document that registers during the teardown keeps the old epoch (it reloads into this reset's
    // boot) and cannot trigger a second reset.
    inFlight = performReset().finally(() => {
      epoch += 1
      inFlight = null
    })
    await inFlight

    return { ok: true, performed: true }
  }

  return { epoch: () => epoch, request }
}
