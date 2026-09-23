// The ONE queue behind "Reopen Saved Session Windows" and "Retry remaining
// windows" (Codex A4 round 3 #3). Both commands used to have their own
// single-flight guard, so a Reopen and a Resume could each admit an unready
// window at the same time. Now there is exactly one active queue: a Reopen
// while it runs MERGES its entries into it (by window id), a Resume while it
// runs JOINS it, and neither ever runs a second queue in parallel.
//
// Windows are opened strictly one at a time (runEntry resolves once the window
// is verified ready, or reports why not). A failed window halts the queue; the
// failed entry and everything after it are kept for Resume.

export type ReopenStep = { ok: true } | { ok: false; profile: null | string; reason: string } | 'skipped'

export interface ReopenHalt {
  profile: null | string
  reason: string
  restoredCount: number
  totalCount: number
}

export function createReopenQueue<E extends { windowId: string }>(deps: {
  runEntry: (entry: E) => Promise<ReopenStep>
  onHalt: (halt: ReopenHalt) => void
  settle?: () => Promise<void>
  /** "Close and Forget This Window" (Codex A4 round 3 #2): never (re)opened. */
  isForgotten?: (windowId: string) => boolean
}) {
  const cancelled = (windowId: string) => deps.isForgotten?.(windowId) === true
  let pending: E[] = []
  let halted: E[] | null = null
  let active: Promise<void> | null = null
  let restored = 0
  let total = 0

  function enqueue(entries: E[]) {
    const queued = new Set(pending.map(entry => entry.windowId))

    for (const entry of entries) {
      if (!queued.has(entry.windowId) && !cancelled(entry.windowId)) {
        queued.add(entry.windowId)
        pending.push(entry)
        total += 1
      }
    }
  }

  async function drain() {
    while (pending.length > 0) {
      const entry = pending.shift() as E

      // Re-checked immediately before opening: the entry was captured earlier,
      // and its window may have been forgotten since (during a settle or while
      // an earlier window was being verified).
      if (cancelled(entry.windowId)) {
        total -= 1

        continue
      }

      const step = await deps.runEntry(entry)

      // A window forgotten while it was being verified closed on purpose: that
      // is not a failure to halt on (nor an entry to keep for Resume).
      if (step !== 'skipped' && 'reason' in step && !cancelled(entry.windowId)) {
        halted = [entry, ...pending]
        pending = []
        deps.onHalt({ restoredCount: restored, totalCount: total, profile: step.profile, reason: step.reason })

        return
      }

      restored += 1

      if (pending.length > 0) {
        await deps.settle?.()
      }
    }
  }

  function start(): Promise<void> {
    if (!active) {
      active = drain().finally(() => {
        active = null
      })
    }

    return active
  }

  function reset() {
    halted = null
    restored = 0
    total = 0
  }

  return {
    /** Reopen: a fresh queue from `entries`, or merged into the running one. */
    reopen(entries: E[]): Promise<void> {
      if (!active) {
        reset()
      }

      enqueue(entries)

      return start()
    },
    /** Resume a halted queue; joins the running one instead of starting a second. */
    async resume(): Promise<{ error?: string; ok: boolean }> {
      if (active) {
        await active

        return { ok: true }
      }

      if (!halted || halted.length === 0) {
        return { ok: false, error: 'nothing to resume' }
      }

      const entries = halted

      reset()
      enqueue(entries)
      await start()

      return { ok: true }
    },
    /** A forgotten window leaves the active AND the halted queue. */
    forget(windowId: string) {
      const before = pending.length

      pending = pending.filter(entry => entry.windowId !== windowId)
      total -= before - pending.length

      if (halted) {
        halted = halted.filter(entry => entry.windowId !== windowId)
      }
    },
    get isActive() {
      return active !== null
    },
    get haltedIds(): string[] {
      return (halted ?? []).map(entry => entry.windowId)
    }
  }
}
