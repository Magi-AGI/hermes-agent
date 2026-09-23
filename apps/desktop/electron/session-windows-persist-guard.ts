/**
 * Computes the next saved session-window layout (session-windows-state.json).
 *
 * Lake's rules (sync A4):
 * - A saved entry is removed ONLY by an explicit individual close of that
 *   window — never by age, count, app shutdown or a bulk close.
 * - Writes that add windows always go through, even after a crash cascade:
 *   new windows are always recorded.
 * - A write that would remove more than one saved entry at once is refused
 *   (a cascade, not a deliberate close); its additions/updates still apply.
 *
 * So the saved list is a MERGE, never re-derived from the live windows: live
 * windows update their own entry (by per-window id) or are appended, saved
 * entries whose windows are not open stay, and only the ids in `closedIds`
 * (the windows individually closed since the last write, gathered by main.ts
 * over one debounce) can be removed.
 *
 * Previously the list was re-derived from the live set and a count guard
 * refused any write more than one smaller than the saved list. After a
 * partial crash cascade that guard kept refusing EVERY later write,
 * including ones that only added new windows, until the live count caught
 * back up (history: hermes-session-window-list-wipe.md, 21/31/13 -> 1).
 */
interface SavedWindow {
  windowId: string
}

export function mergeSavedWindows<T extends SavedWindow>(
  saved: T[],
  live: T[],
  closedIds: Iterable<string>
): { entries: T[]; refusedRemovals: string[] } {
  const liveById = new Map(live.map(entry => [entry.windowId, entry]))
  const closed = Array.from(new Set(closedIds)).filter(id => !liveById.has(id))
  const removable = new Set(closed.length === 1 ? closed : [])
  const refusedRemovals = closed.length > 1 ? closed.filter(id => saved.some(e => e.windowId === id)) : []

  const entries: T[] = []
  const seen = new Set<string>()

  for (const entry of saved) {
    if (removable.has(entry.windowId) || seen.has(entry.windowId)) {
      continue
    }

    seen.add(entry.windowId)
    entries.push(liveById.get(entry.windowId) ?? entry)
  }

  for (const entry of live) {
    if (!seen.has(entry.windowId)) {
      seen.add(entry.windowId)
      entries.push(entry)
    }
  }

  return { entries, refusedRemovals }
}
