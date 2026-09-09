/**
 * Guards `persistSessionWindowsState` (main.ts) against clobbering a saved
 * session-window layout with a partial bulk-close cascade.
 *
 * `persistSessionWindowsState` re-derives the saved set from whatever windows
 * are still live each time it runs, debounced by 250ms. The design already
 * refuses to write an exactly-empty set (a real "every window closed" bulk
 * teardown), on the assumption a deliberate single-window close only ever
 * drops the live count by exactly 1 within one debounce cycle — see
 * `wireSessionWindowPersistence`'s 'closed' handler in main.ts.
 *
 * That assumption breaks when a bulk teardown doesn't land on exactly zero:
 * a crash cascade, an OS session-end/shutdown, or any window-teardown path
 * that doesn't route through `closeAllSessionWindows()`'s own
 * `suppressPersistOnClose` sweep can leave one or two stragglers still open
 * a moment longer, landing the debounced write on a small nonzero count that
 * sails past the empty-guard. Real-world recurrences: a saved set shrinking
 * from 21, 31, and 13 entries down to 1 — never landing on the empty-guard's
 * exact-zero case.
 *
 * See DESIGN-SESSION-OWNERSHIP-DOUBLE-CLAIM-2026-09-08.md's sibling
 * investigation session for the live incident this was extracted from, and
 * hermes-session-window-list-wipe.md (memory) for the three prior
 * occurrences before this fix.
 */
export function shouldSkipShrinkingSessionWindowsWrite(previousCount: number, newCount: number): boolean {
  return previousCount - newCount > 1
}
