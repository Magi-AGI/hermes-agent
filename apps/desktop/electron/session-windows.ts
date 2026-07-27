// Secondary "session windows" — one extra OS window per chat so a user can
// work with multiple chats side by side. The pure, Electron-free pieces live
// here so they can be unit-tested with node --test (mirroring how the rest of
// electron/*.ts splits testable logic out of the main.ts monolith).

import { pathToFileURL } from 'node:url'

import { computeWindowOptions, sanitizeWindowState } from './window-state'

// Secondary windows open at the minimum usable size — a compact side panel for
// subagent watch / cmd-click session pop-out, not a second full desktop.
const SESSION_WINDOW_MIN_WIDTH = 420
const SESSION_WINDOW_MIN_HEIGHT = 620

// Shared webPreferences for every window that renders the chat transcript — the
// primary window AND the secondary session windows. Keeping it in one place is
// the whole point: the two BrowserWindow definitions in main.ts used to be
// hand-copied, and the secondary windows silently lost `backgroundThrottling:
// false`, so a streamed answer stalled until the window regained focus.
//
// `backgroundThrottling: false` is load-bearing: the transcript streams to the
// screen through a requestAnimationFrame-gated flush, which Chromium pauses for
// blurred/occluded windows. A streaming chat app must keep painting in the
// background, so every chat window opts out. The preload path is injected
// because it depends on the Electron entry's __dirname.
function chatWindowWebPreferences(preloadPath: string) {
  return {
    preload: preloadPath,
    contextIsolation: true,
    webviewTag: true,
    sandbox: true,
    nodeIntegration: false,
    devTools: true,
    backgroundThrottling: false
  }
}

// Build the renderer URL for a secondary window. The renderer uses a
// HashRouter, so the session route lives after the '#'. The `?win=secondary`
// flag MUST sit in the query string BEFORE the '#': anything after the '#' is
// treated as the route by HashRouter and would break routeSessionId(). The
// renderer reads the flag from window.location.search to suppress the install /
// onboarding overlays and the global session sidebar. `watch=1` marks a
// spectator window (e.g. a running subagent's session): the renderer resumes it
// lazily so the gateway never builds an agent just to stream into it.
function buildSessionWindowUrl(sessionId: string, { devServer, rendererIndexPath, watch }: any = {}) {
  const query = `?win=secondary${watch ? '&watch=1' : ''}`
  const route = `#/${encodeURIComponent(sessionId)}`

  if (devServer) {
    const base = devServer.endsWith('/') ? devServer.slice(0, -1) : devServer

    return `${base}/${query}${route}`
  }

  return `${pathToFileURL(rendererIndexPath).toString()}${query}${route}`
}

// Full "instance" windows (⌘⇧N / the "New Window" command) open a complete app
// peer, not a compact chat. Cascade each one off its source window's bounds so a
// new window doesn't land exactly on top of the one it was spawned from. Pure so
// it's unit-testable; the Electron glue (reading the focused window's bounds,
// constructing the BrowserWindow) stays in main.ts. `base` is the source
// window's current bounds, or null when there's no live source window — then the
// persisted primary geometry (`fallback`) is used as-is.
const INSTANCE_CASCADE_OFFSET = 32

function instanceWindowBounds(base: { x: number; y: number; width: number; height: number } | null, fallback: any) {
  if (!base) {
    return fallback
  }

  return {
    width: base.width,
    height: base.height,
    x: base.x + INSTANCE_CASCADE_OFFSET,
    y: base.y + INSTANCE_CASCADE_OFFSET
  }
}

// A small registry keyed by sessionId that guarantees one window per chat:
// opening a session that already has a live window focuses it instead of
// spawning a duplicate, and a window removes itself from the registry when it
// closes. The actual BrowserWindow construction is injected (the `factory`) so
// this module stays free of Electron and is unit-testable.
//
// Each entry also carries the `watch` flag it was opened with (a spectator
// window on a subagent run) — close-all/persistence consumers need it to
// exclude watch windows from the "reopen saved windows" set. The flag is
// fixed at creation time: re-requesting the same sessionId with a different
// watch value still just focuses the existing window (one window per
// sessionId is the invariant; watch does not "upgrade" or replace it).
function createSessionWindowRegistry() {
  const windows = new Map()

  function openOrFocus(sessionId, factory, { watch = false }: { watch?: boolean } = {}) {
    const key = typeof sessionId === 'string' ? sessionId.trim() : ''

    if (!key) {
      return null
    }

    const existing = windows.get(key)

    if (existing && !existing.win.isDestroyed()) {
      // Focus-or-create: never duplicate a window for the same chat.
      if (typeof existing.win.isMinimized === 'function' && existing.win.isMinimized()) {
        existing.win.restore?.()
      }

      if (typeof existing.win.isVisible === 'function' && !existing.win.isVisible()) {
        existing.win.show?.()
      }

      existing.win.focus?.()

      return existing.win
    }

    const win = factory(key)

    if (!win) {
      return null
    }

    windows.set(key, { win, watch: Boolean(watch) })

    // Self-cleanup on close so the registry never holds a destroyed window.
    win.on?.('closed', () => {
      if (windows.get(key)?.win === win) {
        windows.delete(key)
      }
    })

    return win
  }

  return {
    openOrFocus,
    get: key => windows.get(key)?.win,
    has: key => windows.has(key),
    get size() {
      return windows.size
    },
    // Metadata snapshot for close-all / persistence — drops anything already
    // destroyed as a defensive measure (should be unreachable: 'closed' self-
    // cleans the map).
    entries: () =>
      Array.from(windows.entries())
        .filter(([, entry]) => entry.win && !entry.win.isDestroyed())
        .map(([sessionId, entry]) => ({ sessionId, win: entry.win, watch: entry.watch })),
    // Closes every live window in the registry (including watch windows —
    // "close all" means all session pop-outs, not just the reopenable set).
    // Each window's own 'closed' handler removes it from the map.
    closeAll: () => {
      for (const { win } of Array.from(windows.values())) {
        if (win && !win.isDestroyed()) {
          win.close?.()
        }
      }
    }
  }
}

// computeWindowOptions/sanitizeWindowState default to the MAIN window's
// minimums (400×620) — calling them unparameterized for a session window would
// silently under-floor the width by 20px. These wrappers pin the session-window
// minimums so callers can't get this wrong.
function sanitizeSessionWindowState(raw?: any) {
  return sanitizeWindowState(raw, { minWidth: SESSION_WINDOW_MIN_WIDTH, minHeight: SESSION_WINDOW_MIN_HEIGHT })
}

function computeSessionWindowOptions(state, displays) {
  return computeWindowOptions(state, displays, {
    minWidth: SESSION_WINDOW_MIN_WIDTH,
    minHeight: SESSION_WINDOW_MIN_HEIGHT
  })
}

// Validate one persisted "reopen saved session windows" entry. Requires a
// non-empty sessionId plus sane geometry (via sanitizeSessionWindowState);
// drops anything else rather than falling back to main-window-sized defaults.
function sanitizeSessionWindowEntry(raw?: any) {
  const sessionId = typeof raw?.sessionId === 'string' ? raw.sessionId.trim() : ''

  if (!sessionId) {
    return null
  }

  const geometry = sanitizeSessionWindowState(raw)

  if (!geometry) {
    return null
  }

  return { sessionId, ...geometry }
}

export {
  buildSessionWindowUrl,
  chatWindowWebPreferences,
  computeSessionWindowOptions,
  createSessionWindowRegistry,
  instanceWindowBounds,
  sanitizeSessionWindowEntry,
  sanitizeSessionWindowState,
  SESSION_WINDOW_MIN_HEIGHT,
  SESSION_WINDOW_MIN_WIDTH
}
