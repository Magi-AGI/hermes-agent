import { notifyError } from './notifications'

// Window flag set by the Electron main process when it opens a standalone
// session window (see electron/main.ts buildSessionWindowUrl). It rides in the
// query string BEFORE the HashRouter '#', so we read it from location.search,
// never from the router. A "secondary" window renders a single chat without the
// global session sidebar or the install / onboarding overlays.
const SECONDARY_WINDOW_FLAG = 'secondary'
const NEW_SESSION_WINDOW_FLAG = '1'

let secondaryWindowCache: boolean | null = null

export function isSecondaryWindow(): boolean {
  if (secondaryWindowCache !== null) {
    return secondaryWindowCache
  }

  let result = false

  try {
    result = new URLSearchParams(window.location.search).get('win') === SECONDARY_WINDOW_FLAG
  } catch {
    result = false
  }

  secondaryWindowCache = result

  return result
}

let newSessionWindowCache: boolean | null = null

export function isNewSessionWindow(): boolean {
  if (newSessionWindowCache !== null) {
    return newSessionWindowCache
  }

  let result = false

  try {
    result = new URLSearchParams(window.location.search).get('new') === NEW_SESSION_WINDOW_FLAG
  } catch {
    result = false
  }

  newSessionWindowCache = result

  return result
}

// Shared "does this secondary window show the ChatHeader" predicate — the
// single source of truth consumed by both ChatHeader (the title chip/menu)
// and ThreadMessageList (the top-gap + sticky-offset + drag-mask it only
// needs when there's no header to serve that role). A secondary window is
// headerless exactly while it has no resolvable session: a new-session draft
// before the first message creates a real session, or (in principle) any
// other still-blank pop-out. Once a session exists — whether the window
// opened already keyed to one, or a draft just created one — it gets a
// header, live rename updates, and a real document.title, matching how the
// main window has always worked.
export function isHeaderlessSecondaryWindow(hasSession: boolean): boolean {
  return isSecondaryWindow() && !hasSession
}

let watchWindowCache: boolean | null = null

// A "watch" window spectates a session that is being driven elsewhere (a
// running subagent). It resumes lazily — the gateway registers history + a
// transport for the live mirror without building an agent, so opening it is
// cheap even while the backend is busy running the delegation.
export function isWatchWindow(): boolean {
  if (watchWindowCache !== null) {
    return watchWindowCache
  }

  let result = false

  try {
    result = new URLSearchParams(window.location.search).get('watch') === '1'
  } catch {
    result = false
  }

  watchWindowCache = result

  return result
}

// True when running inside the Electron desktop shell (the preload bridge is
// present). The "open in new window" affordance is desktop-only.
export function canOpenSessionWindow(): boolean {
  return typeof window !== 'undefined' && typeof window.hermesDesktop?.openSessionWindow === 'function'
}

type WindowOpenResult = { ok: boolean; error?: string } | undefined

// Run a window-open bridge call, surfacing any failure as a toast. Shared by the
// session pop-out and the new-session pop-out.
async function openWindow(call: () => Promise<WindowOpenResult>, failMessage: string): Promise<void> {
  try {
    const result = await call()

    if (!result?.ok) {
      notifyError(new Error(result?.error || 'unknown error'), failMessage)
    }
  } catch (err) {
    notifyError(err, failMessage)
  }
}

// Open (or focus) a standalone OS window for a single chat session. No-ops
// gracefully outside Electron so callers can wire it unconditionally.
// `watch: true` opens a spectator window (lazy resume, live-mirror stream).
export async function openSessionInNewWindow(sessionId: string, opts?: { watch?: boolean }): Promise<void> {
  if (!sessionId || !canOpenSessionWindow()) {
    return
  }

  await openWindow(() => window.hermesDesktop.openSessionWindow(sessionId, opts), 'Could not open chat in a new window')
}

// Open a fresh compact window on the new-session draft.
export async function openNewSessionInNewWindow(): Promise<void> {
  if (!canOpenSessionWindow() || typeof window.hermesDesktop.openNewSessionWindow !== 'function') {
    return
  }

  await openWindow(() => window.hermesDesktop.openNewSessionWindow(), 'Could not open new session window')
}

// Close every live session pop-out (including watch/spectator windows). No-op
// outside Electron.
export async function closeAllSessionWindows(): Promise<void> {
  if (!canOpenSessionWindow() || typeof window.hermesDesktop.closeAllSessionWindows !== 'function') {
    return
  }

  await openWindow(() => window.hermesDesktop.closeAllSessionWindows(), 'Could not close session windows')
}

// Explicit-action restore of the last-closed (non-watch) session windows, at
// their saved positions. Never runs automatically — only via this call.
export async function reopenSessionWindows(): Promise<void> {
  if (!canOpenSessionWindow() || typeof window.hermesDesktop.reopenSessionWindows !== 'function') {
    return
  }

  await openWindow(() => window.hermesDesktop.reopenSessionWindows(), 'Could not reopen session windows')
}
