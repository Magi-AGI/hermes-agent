/**
 * The saved session-window layout (session-windows-state.json).
 *
 * Lake's rule (sync A4): CLOSES NEVER REMOVE. No close of any kind — the X,
 * Ctrl/Cmd+W, Alt+F4, a taskbar close (single or "Close all windows"), app quit,
 * OS shutdown, a crash, destroy(), Close All Session Windows — removes a saved
 * window entry. A saved entry is removed ONLY by the explicit user command
 * "Close and Forget This Window". Closed windows therefore come back on
 * Reopen Saved Session Windows, by design.
 *
 * The saved list is a MERGE, never re-derived from the live windows: live
 * windows update their own entry (by per-window id) or are appended; saved
 * entries whose windows are not open stay; only `forgetIds` are removed.
 */
interface SavedWindow {
  windowId: string
}

export function mergeSavedWindows<T extends SavedWindow>(
  saved: T[],
  live: T[],
  forgetIds: Iterable<string>
): { entries: T[]; removed: string[] } {
  const removable = new Set(forgetIds)
  // A forgotten window never re-enters the list, even while it is still closing.
  const liveById = new Map(live.filter(entry => !removable.has(entry.windowId)).map(entry => [entry.windowId, entry]))

  const entries: T[] = []
  const removed: string[] = []
  const seen = new Set<string>()

  for (const entry of saved) {
    if (removable.has(entry.windowId)) {
      removed.push(entry.windowId)

      continue
    }

    if (seen.has(entry.windowId)) {
      continue
    }

    seen.add(entry.windowId)
    entries.push(liveById.get(entry.windowId) ?? entry)
  }

  for (const entry of liveById.values()) {
    if (!seen.has(entry.windowId)) {
      seen.add(entry.windowId)
      entries.push(entry)
    }
  }

  return { entries, removed }
}

/**
 * One persist decision. A failed load (anything but a missing file — see
 * loadSavedWindowsFile) writes nothing and RETAINS the pending forgets for the
 * next attempt; otherwise the merged list is written when it changed, and the
 * applied forgets may be cleared once that write succeeds.
 */
export function planSavedWindowsWrite<T extends SavedWindow>(
  loaded: { entries: T[]; ok: true } | { error: string; ok: false },
  live: T[],
  forgetIds: string[]
): { clearForgets: boolean; error?: string; write: null | string } {
  if ('error' in loaded) {
    return { write: null, clearForgets: false, error: loaded.error }
  }

  const next = JSON.stringify(mergeSavedWindows(loaded.entries, live, forgetIds).entries, null, 2)

  return { write: next === JSON.stringify(loaded.entries, null, 2) ? null : next, clearForgets: true }
}

export type WindowCloseReason =
  | 'app-quit'
  | 'close-all-session-windows'
  | 'crash'
  | 'destroy'
  | 'os-shutdown'
  | 'reopen-teardown'
  | 'taskbar-close'
  | 'taskbar-close-all'
  | 'user-alt-f4'
  | 'user-ctrl-w'
  | 'user-x'

/**
 * The saved layout as main.ts drives it. `load`/`write` are the file (injected
 * for unit tests); `live()` returns the open restorable windows' entries.
 */
export function createSavedWindowLayout<T extends SavedWindow>(deps: {
  load: () => { entries: T[]; ok: true } | { error: string; ok: false }
  write: (text: string) => void
  live: () => T[]
  log?: (message: string) => void
}) {
  const pendingForgets = new Set<string>()

  // Pending forgets are cleared ONLY after a write succeeds (or is a no-op); a
  // failed read or write retains them for the next attempt.
  function persist(): boolean {
    const forgets = Array.from(pendingForgets)

    try {
      const plan = planSavedWindowsWrite(deps.load(), deps.live(), forgets)

      if (plan.error) {
        deps.log?.(`[session-windows-state] not writing: cannot read saved layout (${plan.error})`)

        return false
      }

      if (plan.write !== null) {
        deps.write(plan.write)
      }

      for (const id of forgets) {
        pendingForgets.delete(id)
      }

      return true
    } catch (err: any) {
      deps.log?.(`[session-windows-state] persist failed: ${err?.message || err}`)

      return false
    }
  }

  return {
    persist,
    /** "Close and Forget This Window": the only way a saved entry is removed. */
    forget(windowId: string): boolean {
      pendingForgets.add(windowId)

      return persist()
    },
    isForgotten: (windowId: string) => pendingForgets.has(windowId),
    /** Any close, for any reason: records the layout as it stands; never removes. */
    onWindowClosed(_windowId: string, _reason: WindowCloseReason): void {
      persist()
    }
  }
}
