// Secondary "session windows" — one extra OS window per chat so a user can
// work with multiple chats side by side. The pure, Electron-free pieces live
// here so they can be unit-tested with node --test (mirroring how the rest of
// electron/*.ts splits testable logic out of the main.ts monolith).

import { randomUUID } from 'node:crypto'
import { pathToFileURL } from 'node:url'

import type { DesktopWindowLaunch } from './desktop-profile'
import { computeWindowOptions, sanitizeWindowState } from './window-state'

// Secondary windows open at the minimum usable size — a compact side panel for
// subagent watch / cmd-click session pop-out, not a second full desktop.
const SESSION_WINDOW_MIN_WIDTH = 420
const SESSION_WINDOW_MIN_HEIGHT = 620

// Shared webPreferences for every window that renders the chat transcript — the
// primary window AND the secondary session windows. Keeping it in one place is
// the whole point: the two BrowserWindow definitions in main.ts used to be
// hand-copied, and the secondary windows silently drifted apart (a streamed
// answer stalled until the window regained focus because one of them lost the
// throttling opt-out).
//
// Background throttling is deliberately NOT set here. It is managed at runtime
// by main.ts (`setBackgroundThrottling` driven by the merged `hermes:active-work`
// reports): while any turn is in flight every chat window is unthrottled so the
// transcript's bounded timer flush keeps painting while blurred, occluded, or
// minimized — and once all turns finish, Chromium's default throttling returns
// so an idle hidden window costs ~nothing. A static `backgroundThrottling:
// false` here would pin `document.visibilityState` to 'visible' forever,
// turning every visibility-gated poll in the renderer into an always-on timer
// (the "Hermes idles at 20% CPU while minimized" bug). The preload path is
// injected because it depends on the Electron entry's __dirname.
//
// `autoplayPolicy: 'no-user-gesture-required'` is load-bearing for voice:
// Chromium's default autoplay policy suspends audio (HTMLAudioElement.play()
// and AudioContext) until the user has interacted with the frame. A voice
// conversation started by the "Hey Hermes" wake word has NO preceding click,
// so the FIRST reply's audio playback was rejected (NotAllowedError, silently
// swallowed) and only turn 2+ spoke — the very "first message in a new voice
// session is silent" bug. Manual voice-start worked only because the button
// click counted as the gesture. This is a native app the user deliberately
// launched; there is no drive-by-autoplay concern to protect against.
//
// `focusOnNavigation: false` keeps renderer-driven work passive. Electron's
// default is true, so an in-page/SPA navigation can activate a blurred chat
// window while its transcript is streaming. Explicit user actions still call
// the main-process window focus paths (session re-open, notification/deep-link,
// app activation), preserving intentional raises without background focus theft.
function chatWindowWebPreferences(preloadPath: string) {
  return {
    preload: preloadPath,
    contextIsolation: true,
    webviewTag: true,
    sandbox: true,
    nodeIntegration: false,
    devTools: true,
    autoplayPolicy: 'no-user-gesture-required' as const,
    focusOnNavigation: false
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
// `profile` names the backend the window must boot against (same carry as the
// HUD's buildHudWindowUrl): without it a pop-out/watch window adopts the
// PRIMARY profile and resolves the session id against the wrong backend
// (#82768, #61286). Absent → unchanged primary adoption.
function buildSessionWindowUrl(sessionId: string, { devServer, profile, rendererIndexPath, watch }: any = {}) {
  const profileKey = typeof profile === 'string' ? profile.trim() : ''
  const query = `?win=secondary${watch ? '&watch=1' : ''}${profileKey ? `&profile=${encodeURIComponent(profileKey)}` : ''}`
  const route = `#/${encodeURIComponent(sessionId)}`

  if (devServer) {
    const base = devServer.endsWith('/') ? devServer.slice(0, -1) : devServer

    return `${base}/${query}${route}`
  }

  return `${pathToFileURL(rendererIndexPath).toString()}${query}${route}`
}

// Full peer windows render the ordinary app shell, so they deliberately do
// not use the `win` query parameter that selects a specialized renderer. The
// separate marker lets the renderer distinguish a peer from the one primary
// app window: app-launch source restoration belongs to the primary only, while
// a peer keeps the already-running backend it joined during boot.
interface InstanceWindowUrlOptions extends Partial<DesktopWindowLaunch> {
  devServer?: string
  rendererIndexPath?: string
  // Magi: a RESTORED peer window opens directly on the session it was showing
  // (hash route, like a session pop-out); a fresh peer omits it and boots on a
  // new chat.
  sessionId?: null | string
}

function buildInstanceWindowUrl({
  connectionId,
  devServer,
  profile,
  profileWindow,
  rendererIndexPath,
  sessionId
}: InstanceWindowUrlOptions = {}) {
  const query = `?peer=1${profile ? `&profile=${encodeURIComponent(profile)}&connectionId=${encodeURIComponent(connectionId ?? '')}${profileWindow ? '&profileWindow=1' : ''}` : ''}`
  const session = typeof sessionId === 'string' ? sessionId.trim() : ''
  const route = session ? `#/${encodeURIComponent(session)}` : ''

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
// persisted primary geometry (`fallback`) is used as-is. The cascaded rect is
// clamped to the work area it lands on (a source docked at the bottom/right edge
// would otherwise push the new window past the screen); with no matching
// display it is kept as computed.
const INSTANCE_CASCADE_OFFSET = 32

function instanceWindowBounds(
  base: { x: number; y: number; width: number; height: number } | null,
  fallback: any,
  displays: any[] = []
) {
  if (!base) {
    return fallback
  }

  const bounds = {
    width: base.width,
    height: base.height,
    x: base.x + INSTANCE_CASCADE_OFFSET,
    y: base.y + INSTANCE_CASCADE_OFFSET
  }

  return { ...bounds, ...computeWindowOptions(bounds, displays) }
}

// Registry of every restorable app window (session pop-outs, watch windows and
// full peer windows), keyed by a per-WINDOW id — never by session (Magi, sync
// A4: two windows on one session are two windows and two saved entries). The
// actual BrowserWindow construction is injected (the `factory`) so this module
// stays free of Electron and is unit-testable.
//
// openOrFocus(windowId) focuses a window only when that SAME window id is
// already live: re-opening a saved window that is still open, or a caller that
// deliberately keys by something stable (watch windows use `watch:<session>`).
// A new pop-out or peer window gets a fresh id, so it is always a new window.
//
// Returns { win, created } — `created` distinguishes "focused an existing live
// window" from "factory actually spawned a new one". Callers that need to know
// whether a window is brand-new (e.g. the reopen queue: a fast-path readiness
// check is only valid for a window that isn't going to redo its boot sequence
// — see session-reopen-gate.ts) must use `created`, never
// BrowserWindow.webContents.isLoading() as a proxy for it.
type RestorableWindowKind = 'peer' | 'session'

interface RestorableWindowMeta {
  connectionId?: null | string
  kind?: RestorableWindowKind
  profile?: null | string
  profileWindow?: boolean
  sessionId?: null | string
  watch?: boolean
}

function newWindowId(): string {
  return randomUUID()
}

function watchWindowId(sessionId: string): string {
  return `watch:${sessionId}`
}

// The window id a session pop-out opens under. Restoring a saved window reuses
// its saved id; a watch window is keyed by its session (a spectator, not a
// layout entry, keeps focus-existing); every other open is a NEW window.
function sessionWindowIdForOpen(sessionId: string, { watch = false, windowId }: { watch?: boolean; windowId?: string } = {}) {
  return windowId || (watch ? watchWindowId(sessionId) : newWindowId())
}

// The persisted shape of one live registry entry.
function toSavedWindowEntry(
  entry: { windowId: string; kind: RestorableWindowKind; sessionId: null | string; profile: null | string; connectionId?: null | string; profileWindow?: boolean },
  bounds: { x: number; y: number; width: number; height: number },
  isMaximized: boolean
) {
  return {
    windowId: entry.windowId,
    kind: entry.kind,
    sessionId: entry.sessionId ?? null,
    x: bounds.x,
    y: bounds.y,
    width: bounds.width,
    height: bounds.height,
    isMaximized,
    profile: entry.profile ?? null,
    ...(entry.kind === 'peer' ? { connectionId: entry.connectionId ?? null, profileWindow: entry.profileWindow === true } : {})
  }
}

function createSessionWindowRegistry() {
  const windows = new Map()

  function openOrFocus(windowId, factory, meta: RestorableWindowMeta = {}) {
    const key = typeof windowId === 'string' ? windowId.trim() : ''

    if (!key) {
      return { win: null, created: false }
    }

    const existing = windows.get(key)

    if (existing && !existing.win.isDestroyed()) {
      if (typeof existing.win.isMinimized === 'function' && existing.win.isMinimized()) {
        existing.win.restore?.()
      }

      if (typeof existing.win.isVisible === 'function' && !existing.win.isVisible()) {
        existing.win.show?.()
      }

      existing.win.focus?.()

      return { win: existing.win, created: false }
    }

    const win = factory(key)

    if (!win) {
      return { win: null, created: false }
    }

    const sessionId = typeof meta.sessionId === 'string' ? meta.sessionId.trim() : ''

    windows.set(key, {
      win,
      kind: meta.kind === 'peer' ? 'peer' : 'session',
      sessionId: sessionId || null,
      watch: Boolean(meta.watch),
      profile: meta.profile || null,
      connectionId: meta.connectionId ?? null,
      profileWindow: Boolean(meta.profileWindow)
    })

    // Self-cleanup on close so the registry never holds a destroyed window.
    win.on?.('closed', () => {
      if (windows.get(key)?.win === win) {
        windows.delete(key)
      }
    })

    return { win, created: true }
  }

  function windowIdOf(win): null | string {
    for (const [key, entry] of windows) {
      if (entry.win === win) {
        return key
      }
    }

    return null
  }

  return {
    openOrFocus,
    windowIdOf,
    // An open window's current route: its launch route until its renderer
    // reports an ESTABLISHED one (setRoute), then the session-owner route it
    // is on now (Codex A4 round 4 #2). Null for an unregistered window.
    routeOf: (win): null | { connectionId: null | string; profile: null | string } => {
      const key = windowIdOf(win)
      const entry = key ? windows.get(key) : null

      return entry ? { profile: entry.profile ?? null, connectionId: entry.connectionId ?? null } : null
    },
    get: key => windows.get(key)?.win,
    has: key => windows.has(key),
    get size() {
      return windows.size
    },
    // A peer window navigates between sessions; its saved entry follows the
    // session it is currently showing. Returns true when it changed.
    // The session a window is showing and that session's resolved OWNER route
    // (connection/profile) are one unit (Codex A4 #5): they are always updated
    // together, so a saved entry can never pair a session with another
    // backend's route. Returns true when anything changed.
    //
    // Only an ESTABLISHED route is authoritative (Codex A4 round 3 #4): until
    // the window's boot has established the route it was launched on, its
    // renderer still holds initial defaults (the default profile, no
    // connection). Those never overwrite the launch route main recorded.
    setRoute: (
      win,
      report: {
        connectionId?: null | string
        established?: boolean
        profile?: null | string
        sessionId?: null | string
      }
    ): boolean => {
      const key = windowIdOf(win)
      const entry = key ? windows.get(key) : null

      if (!entry || report?.established !== true) {
        return false
      }

      const clean = (v: unknown) => (typeof v === 'string' && v.trim() ? v.trim() : null)

      const next = {
        sessionId: clean(report?.sessionId),
        profile: clean(report?.profile),
        connectionId: clean(report?.connectionId)
      }

      // A session pop-out passing through a fresh draft (e.g. /new before the
      // first send) keeps its last session and route until the draft becomes a
      // session: its saved entry must stay reopenable (Codex A4 #6). A peer
      // showing a fresh chat is a valid saved state and is recorded as such.
      if (entry.kind === 'session' && !next.sessionId) {
        return false
      }

      if (
        entry.sessionId === next.sessionId &&
        entry.profile === next.profile &&
        (entry.connectionId ?? null) === next.connectionId
      ) {
        return false
      }

      entry.sessionId = next.sessionId
      entry.profile = next.profile
      entry.connectionId = next.connectionId

      return true
    },
    // Metadata snapshot for close-all / persistence — drops anything already
    // destroyed as a defensive measure (should be unreachable: 'closed' self-
    // cleans the map).
    entries: () =>
      Array.from(windows.entries())
        .filter(([, entry]) => entry.win && !entry.win.isDestroyed())
        .map(([windowId, entry]) => ({
          windowId,
          kind: entry.kind,
          sessionId: entry.sessionId,
          win: entry.win,
          watch: entry.watch,
          profile: entry.profile ?? null,
          connectionId: entry.connectionId ?? null,
          profileWindow: entry.profileWindow
        })),
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

// Validate one persisted "reopen saved windows" entry. Every entry carries its
// own `windowId` and a `kind` ('session' pop-out or 'peer' full window). A
// session pop-out requires a sessionId; a peer may have none (it was showing a
// fresh chat). Sane geometry is required (via sanitizeSessionWindowState);
// anything else is dropped rather than falling back to main-window defaults.
//
// Migration (Magi, sync A4): files written before per-window ids were keyed by
// session and carry no windowId/kind. They are read as session pop-outs with
// the stable id `legacy:<sessionId>` (stable across reads until the file is
// next written with real ids), so no saved entry is lost on upgrade.
function sanitizeSessionWindowEntry(raw?: any) {
  const kind: RestorableWindowKind = raw?.kind === 'peer' ? 'peer' : 'session'
  const sessionId = typeof raw?.sessionId === 'string' ? raw.sessionId.trim() : ''

  if (kind === 'session' && !sessionId) {
    return null
  }

  const geometry = sanitizeSessionWindowState(raw)

  if (!geometry) {
    return null
  }

  const rawId = typeof raw?.windowId === 'string' ? raw.windowId.trim() : ''
  const windowId = rawId || (sessionId ? `legacy:${sessionId}` : '')

  if (!windowId) {
    return null
  }

  const profile = typeof raw?.profile === 'string' ? raw.profile.trim() : ''
  const connectionId = typeof raw?.connectionId === 'string' && raw.connectionId.trim() ? raw.connectionId.trim() : null

  return {
    windowId,
    kind,
    sessionId: sessionId || null,
    ...geometry,
    profile: profile || null,
    ...(kind === 'peer' ? { connectionId, profileWindow: raw?.profileWindow === true } : {})
  }
}

// Loads session-windows-state.json. ONLY a missing file (ENOENT) means an empty
// layout; any other read error, unparsable JSON or a non-array is a FAILURE, and
// the caller must not write a replacement (it would erase the saved layout; Codex
// A4 review). `read` is injected (fs.readFileSync in main.ts) for unit tests.
function loadSavedWindowsFile(
  read: () => string
): { entries: ReturnType<typeof readSavedWindowEntries>; ok: true } | { error: string; ok: false } {
  let text: string

  try {
    text = read()
  } catch (err: any) {
    if (err?.code === 'ENOENT') {
      return { ok: true, entries: [] }
    }

    return { ok: false, error: `read failed: ${err?.message || err}` }
  }

  let raw: unknown

  try {
    raw = JSON.parse(text)
  } catch (err: any) {
    return { ok: false, error: `unparsable: ${err?.message || err}` }
  }

  if (!Array.isArray(raw)) {
    return { ok: false, error: 'not a list' }
  }

  return { ok: true, entries: readSavedWindowEntries(raw) }
}

// Read side of the saved list: sanitize every entry and keep window ids unique
// (a hand-edited or legacy file with two `legacy:` rows for one session keeps
// both, suffixing the later id).
function readSavedWindowEntries(raw: unknown) {
  if (!Array.isArray(raw)) {
    return []
  }

  const seen = new Set<string>()
  const out = []

  for (const item of raw) {
    const entry = sanitizeSessionWindowEntry(item)

    if (!entry) {
      continue
    }

    let id = entry.windowId

    for (let n = 2; seen.has(id); n++) {
      id = `${entry.windowId}#${n}`
    }

    seen.add(id)
    out.push({ ...entry, windowId: id })
  }

  return out
}

export {
  buildInstanceWindowUrl,
  buildSessionWindowUrl,
  chatWindowWebPreferences,
  computeSessionWindowOptions,
  createSessionWindowRegistry,
  instanceWindowBounds,
  loadSavedWindowsFile,
  newWindowId,
  readSavedWindowEntries,
  type RestorableWindowKind,
  type RestorableWindowMeta,
  sanitizeSessionWindowEntry,
  sanitizeSessionWindowState,
  SESSION_WINDOW_MIN_HEIGHT,
  SESSION_WINDOW_MIN_WIDTH,
  sessionWindowIdForOpen,
  toSavedWindowEntry,
  watchWindowId
}
