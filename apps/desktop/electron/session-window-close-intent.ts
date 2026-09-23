/**
 * Which closes of a restorable window (session pop-out or peer) REMOVE its
 * saved-layout entry (Lake's rule, sync A4: never lose a saved window; remove
 * only on an explicit individual close).
 *
 * Intent is recorded from the window's `close` event — the event Electron emits
 * for a close the window can still cancel — and a removal is committed only
 * when that window then actually reaches `closed` with the intent still
 * standing. Nothing is inferred from timing.
 *
 * COUNTS as individual-close intent (the window's `close` event, while none of
 * the exclusions below hold):
 *   - the window's own close button (Windows titlebar-overlay X, macOS traffic
 *     light, Linux/WSLg app close control -> hermes:window-control 'close')
 *   - the Close Window menu role / its accelerator (Ctrl+W, Cmd+W) and Alt+F4
 *   - closing that one window from its taskbar/dock thumbnail
 *
 * PRESERVES the entry (no intent):
 *   - app quit (any path: menu Quit, tray Quit, closing the last window, update
 *     restart) — the app is quitting when the window closes
 *   - OS logout/shutdown (Windows query-session-end/session-end on any window)
 *   - our own programmatic closes: Close All Session Windows, reopen-failure
 *     teardown (markProgrammatic)
 *   - destroy() and any teardown that never emits `close` (renderer/GPU crash
 *     recovery, process crash): no `close` event, no intent
 *   - a `close` that was cancelled and later completed through one of the
 *     paths above
 *
 * KNOWN AMBIGUITY (documented, not solvable at this layer): Windows' taskbar
 * "Close all windows" delivers an ordinary WM_CLOSE to every window of the
 * group, which Electron surfaces exactly like the X button (Electron 40 has no
 * WM_SYSCOMMAND/source hook). While the app keeps running those closes count as
 * individual closes; if it closes the primary window too, the app quits and
 * the remaining windows are preserved.
 */

interface WindowLike {
  isDestroyed?: () => boolean
}

export function createCloseIntentTracker() {
  const programmatic = new WeakSet<object>()
  const intents = new Map<string, object>()
  let shuttingDown = false

  return {
    /** Our own close/destroy of `win`: never an individual-close intent. */
    markProgrammatic(win: WindowLike, windowId?: string) {
      programmatic.add(win)

      if (windowId) {
        intents.delete(windowId)
      }
    },
    /** OS logout/shutdown observed: no later close is an individual close. */
    markShuttingDown() {
      shuttingDown = true
      intents.clear()
    },
    /** The window's `close` event. */
    onClose(win: WindowLike, windowId: string, { quitting }: { quitting: boolean }) {
      if (quitting || shuttingDown || programmatic.has(win)) {
        intents.delete(windowId)

        return
      }

      intents.set(windowId, win)
    },
    /** The window's `closed` event. True when its saved entry should be removed. */
    onClosed(win: WindowLike, windowId: string, { quitting }: { quitting: boolean }): boolean {
      const intended = intents.get(windowId) === win

      intents.delete(windowId)

      return intended && !quitting && !shuttingDown && !programmatic.has(win)
    }
  }
}
