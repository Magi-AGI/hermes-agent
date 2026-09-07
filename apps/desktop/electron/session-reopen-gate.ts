// Gating logic for the "reopen saved session windows" queue — the pure,
// Electron-free piece lives here so it's unit-testable (mirrors how the rest
// of electron/*.ts splits testable logic out of the main.ts monolith; see
// session-windows.ts / backend-health.ts for the same pattern).
//
// Full design + review history: DESIGN-SESSION-WINDOW-SEQUENTIAL-GATING-2026-09-07.md.
// Short version: the previous gate (waitForSessionWindowGatewayReady) resolved
// on the renderer's gateway WebSocket reaching 'open' — which proves the
// transport connected, not that the window is actually usable — and its 20s
// timeout gave up and opened the next window regardless, i.e. it failed OPEN
// under exactly the condition (an already-struggling shared backend) it
// existed to protect against. This replaces it with a result signal gated on
// actual boot-phase success, plus an independent backend health probe run
// from the main process (so a renderer wedged by a fully-frozen backend can't
// silently stall the gate forever — see the design doc's live py-spy evidence),
// and fails CLOSED: an unhealthy backend halts the queue and reports state
// instead of spawning another window into it.

export const REOPEN_WINDOW_READY_TIMEOUT_MS = 45_000
export const REOPEN_WINDOW_SETTLE_MS = 400

const FUNCTIONAL_READY_CHANNEL = 'hermes:window:functional-ready'

export interface FunctionalReadyPayload {
  ok: boolean
  phase?: string
  profile?: string | null
  sessionId?: string | null
  error?: string
}

export interface ReopenGateResult {
  ok: boolean
  reason?: string
}

export interface ReopenGateWindow {
  isDestroyed: () => boolean
  webContents: { id: number; once: (event: string, listener: (...args: any[]) => void) => void }
  once: (event: string, listener: (...args: any[]) => void) => void
}

export interface ReopenGateIpcMain {
  on: (channel: string, listener: (...args: any[]) => void) => void
  removeListener: (channel: string, listener: (...args: any[]) => void) => void
}

export interface ReopenGateDeps {
  ipcMain: ReopenGateIpcMain
  // Independently confirms the target profile's backend is (or becomes)
  // responsive, decoupled from the window's own self-report. Must resolve
  // once healthy, or reject once genuinely unhealthy past its own budget.
  // Wire this to the EXISTING, already-tested waitForHermesReady() readiness
  // path (backend-health.ts) — do not reinvent it as a raw HTTP poll (that
  // path already prefers /api/health, falls back to /api/status only for old
  // backends, and already handles credentialed/auth-gated probes correctly).
  probeBackendHealth: (profile: string | null, signal: AbortSignal) => Promise<void>
  timeoutMs?: number
}

// `created` distinguishes a window the registry actually spawned from one it
// merely focused (session-windows.ts: openOrFocus's `created` flag). ONLY a
// focused, pre-existing window is safe to resolve immediately — it already
// finished its boot sequence and won't re-run it, so it will never emit a new
// functional-ready signal. A newly-created window must always be proven, and
// must NEVER be fast-pathed via `!win.webContents.isLoading()` — Electron
// gives no synchronous guarantee that a just-constructed window's
// isLoading() reads true before its navigation actually starts, so that
// check (the previous gate's approach) could spuriously resolve on tick 0 for
// a brand-new window too, not just a genuinely pre-existing one.
export function waitForSessionWindowFunctionallyReady(
  win: ReopenGateWindow,
  { created, profile }: { created: boolean; profile: string | null },
  deps: ReopenGateDeps
): Promise<ReopenGateResult> {
  if (!created) {
    return Promise.resolve({ ok: true })
  }

  if (win.isDestroyed()) {
    return Promise.resolve({ ok: true })
  }

  const { ipcMain, probeBackendHealth, timeoutMs = REOPEN_WINDOW_READY_TIMEOUT_MS } = deps
  const wcId = win.webContents.id
  const controller = new AbortController()

  return new Promise(resolve => {
    let settled = false
    let timer: ReturnType<typeof setTimeout>
    // BOTH must confirm before the window releases the queue — a renderer
    // report alone is not sufficient (nor is a health probe alone). Requiring
    // only one defeats the reason the independent probe exists: the whole
    // point is a healthy-looking renderer report is not fully trustworthy on
    // its own (a backend that is fine RIGHT NOW but seizing moments later
    // could let a narrowly-timed renderer success through before the
    // independent check would have caught the same freeze). Triad review
    // round 4, Codex P1 — a renderer ok:true previously resolved immediately,
    // aborting the still-pending health probe and discarding its verdict
    // entirely; that made the "independent" probe load-bearing only in the
    // all-frozen case (no renderer signal ever arrives), not the case this
    // paragraph describes.
    let rendererReady = false
    let healthReady = false

    const finish = (result: ReopenGateResult) => {
      if (settled) {
        return
      }

      settled = true
      controller.abort()
      clearTimeout(timer)
      ipcMain.removeListener(FUNCTIONAL_READY_CHANNEL, onReady)
      resolve(result)
    }

    const succeedIfBothReady = () => {
      if (rendererReady && healthReady) {
        finish({ ok: true })
      }
    }

    // Normalizes both sides the same way ('' / null / undefined all mean the
    // default profile) before comparing, so a plain string mismatch can't
    // slip through as a false negative.
    const normalizeProfile = (p: string | null | undefined) => p || 'default'
    const expectedProfile = normalizeProfile(profile)

    const onReady = (event: { sender: { id: number } }, payload: FunctionalReadyPayload) => {
      if (event.sender.id !== wcId) {
        return
      }

      if (payload?.ok !== true) {
        finish({
          ok: false,
          reason: payload?.error || `window reported not-ready (phase: ${payload?.phase || 'unknown'})`
        })

        return
      }

      // A window that came up connected to the WRONG profile's backend must
      // not release the queue — v1 is profile-only-scoped (see design doc
      // §7.2), and this is the minimum sanity check for that scope: even
      // without full connectionId/registry-scoped routing, an outright
      // profile mismatch on ok:true is a real routing bug, not a pass (triad
      // review round 2, Codex finding #3).
      const reportedProfile = normalizeProfile(payload.profile)

      if (reportedProfile !== expectedProfile) {
        finish({
          ok: false,
          reason: `window reported ready for profile "${reportedProfile}", expected "${expectedProfile}"`
        })

        return
      }

      rendererReady = true
      succeedIfBothReady()
    }

    timer = setTimeout(() => {
      finish({
        ok: false,
        reason: `timed out after ${timeoutMs}ms waiting for the window to become functionally ready`
      })
    }, timeoutMs)

    ipcMain.on(FUNCTIONAL_READY_CHANNEL, onReady)

    win.webContents.once(
      'did-fail-load',
      (_event: unknown, _code: unknown, _desc: unknown, _url: unknown, isMainFrame: boolean) => {
        if (isMainFrame) {
          finish({ ok: false, reason: 'page failed to load' })
        }
      }
    )

    // A window closed mid-wait isn't a backend-health failure — don't halt
    // the whole reopen queue over a window the user (or the app) closed.
    win.once('closed', () => finish({ ok: true }))

    void probeBackendHealth(profile, controller.signal)
      .then(() => {
        healthReady = true
        succeedIfBothReady()
      })
      .catch(err => {
        finish({ ok: false, reason: `backend health check failed: ${err?.message || err}` })
      })
  })
}
