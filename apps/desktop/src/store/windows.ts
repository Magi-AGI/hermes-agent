import { notifyError } from './notifications'

// Window flag set by the Electron main process when it opens a standalone
// session window (see electron/main.cjs buildSessionWindowUrl). It rides in the
// query string BEFORE the HashRouter '#', so we read it from location.search,
// never from the router. A "secondary" window renders a single chat without the
// global session sidebar or the install / onboarding overlays.
const SECONDARY_WINDOW_FLAG = 'secondary'
const NEW_SESSION_WINDOW_FLAG = '1'
const PROFILE_QUERY_PARAM = 'profile'

function windowSearchParams(): URLSearchParams | null {
  try {
    return new URLSearchParams(window.location.search)
  } catch {
    return null
  }
}

let secondaryWindowCache: boolean | null = null

export function isSecondaryWindow(): boolean {
  if (secondaryWindowCache !== null) {
    return secondaryWindowCache
  }

  const params = windowSearchParams()
  const result = params?.get('win') === SECONDARY_WINDOW_FLAG

  secondaryWindowCache = result

  return result
}

let newSessionWindowCache: boolean | null = null

export function isNewSessionWindow(): boolean {
  if (newSessionWindowCache !== null) {
    return newSessionWindowCache
  }

  const params = windowSearchParams()
  const result = params?.get('new') === NEW_SESSION_WINDOW_FLAG

  newSessionWindowCache = result

  return result
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

  const params = windowSearchParams()
  const result = params?.get('watch') === '1'

  watchWindowCache = result

  return result
}

// Owning profile carried by an existing-session pop-out. This lives alongside
// `win=secondary` before the HashRouter '#', so the renderer can boot the
// popped-out window directly against that profile's backend instead of guessing
// from the active/main window profile.
export function secondaryWindowProfile(): string | null {
  if (!isSecondaryWindow() || isNewSessionWindow()) {
    return null
  }

  const profile = windowSearchParams()?.get(PROFILE_QUERY_PARAM)?.trim()

  return profile || null
}

// True when running inside the Electron desktop shell (the preload bridge is
// present). The "open in new window" affordance is desktop-only.
export function canOpenSessionWindow(): boolean {
  return typeof window !== 'undefined' && typeof window.hermesDesktop?.openSessionWindow === 'function'
}

type WindowOpenResult = { ok: boolean; error?: string } | undefined

export interface SessionWindowOptions {
  profile?: null | string
  watch?: boolean
}

function cleanSessionWindowOptions(opts?: SessionWindowOptions): SessionWindowOptions | undefined {
  if (!opts) {
    return undefined
  }

  const profile = typeof opts.profile === 'string' ? opts.profile.trim() : ''

  return {
    ...(opts.watch ? { watch: true } : {}),
    ...(profile ? { profile } : {})
  }
}

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
export async function openSessionInNewWindow(sessionId: string, opts?: SessionWindowOptions): Promise<void> {
  if (!sessionId || !canOpenSessionWindow()) {
    return
  }

  await openWindow(
    () => window.hermesDesktop.openSessionWindow(sessionId, cleanSessionWindowOptions(opts)),
    'Could not open chat in a new window'
  )
}

// Open a fresh compact window on the new-session draft.
export async function openNewSessionInNewWindow(): Promise<void> {
  if (!canOpenSessionWindow() || typeof window.hermesDesktop.openNewSessionWindow !== 'function') {
    return
  }

  await openWindow(() => window.hermesDesktop.openNewSessionWindow(), 'Could not open new session window')
}
