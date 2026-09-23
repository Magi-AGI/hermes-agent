/**
 * Computes the next saved session-window layout (session-windows-state.json).
 *
 * Lake's rules (sync A4): never lose a saved window; remove one ONLY after an
 * explicit individual close; always record new windows.
 *
 * The saved list is a MERGE, never re-derived from the live windows: live
 * windows update their own entry (by per-window id) or are appended; saved
 * entries whose windows are not open (quit, crash, destroy, never reopened)
 * stay; only `closedIds` are removed. `closedIds` must hold ONLY windows whose
 * individual-close intent was recorded (session-window-close-intent.ts) — the
 * cascade protection is that a quit, crash, destroy or programmatic close never
 * produces an id here, not a timing or count heuristic (the earlier 250ms-batch
 * refusal deleted two deliberate closes 300ms apart and kept two deliberate
 * closes 200ms apart; Codex A4 review).
 */
interface SavedWindow {
  windowId: string
}

export function mergeSavedWindows<T extends SavedWindow>(
  saved: T[],
  live: T[],
  closedIds: Iterable<string>
): { entries: T[]; removed: string[] } {
  const liveById = new Map(live.map(entry => [entry.windowId, entry]))
  const removable = new Set(Array.from(closedIds).filter(id => !liveById.has(id)))

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

  for (const entry of live) {
    if (!seen.has(entry.windowId)) {
      seen.add(entry.windowId)
      entries.push(entry)
    }
  }

  return { entries, removed }
}
