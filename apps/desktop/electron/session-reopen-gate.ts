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

export interface ReopenProbeTarget {
  connectionId: null | string
  profile: null | string
}

// The route the reopen probe must check for one saved window: a peer's route
// recorded when it was spawned (exact connection + profile) when available,
// else its saved route; a session pop-out boots by profile (its URL carries no
// connection), so it is probed on that profile's route.
export function reopenProbeTarget(
  entry: { connectionId?: null | string; kind?: string; profile?: null | string },
  recorded: { connectionId?: null | string; profile?: null | string } | null,
  fallbackProfile: string
): ReopenProbeTarget {
  if (entry.kind === 'peer') {
    const route = recorded ?? entry

    return { profile: route.profile || fallbackProfile, connectionId: route.connectionId ?? null }
  }

  return { profile: entry.profile || fallbackProfile, connectionId: null }
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
  // Receives the EXACT route the window was opened on (Codex A4 #7): a peer on a
  // remote connection is probed on that connection, never on the local backend
  // of the same profile name.
  probeBackendHealth: (target: ReopenProbeTarget, signal: AbortSignal) => Promise<void>
  timeoutMs?: number
  // The window's already-recorded verdict (createWindowReadinessTracker), if it
  // reported before this wait began (an already-open window).
  knownVerdict?: FunctionalReadyPayload | null
}

// Every window's latest functional-ready verdict, recorded for the life of the
// app (Codex A4 #4). The reopen queue used to trust `created: false` (a window
// it only focused) as ready, which is wrong for a window that is still booting
// or already failed. A window reports once per load (useReopenReadySignal), so
// an already-open window's verdict is looked up here; one that has not reported
// yet is waited for like a new window.
export function createWindowReadinessTracker(ipcMain: ReopenGateIpcMain) {
  const verdicts = new Map<number, FunctionalReadyPayload>()

  ipcMain.on(FUNCTIONAL_READY_CHANNEL, (event: { sender: { id: number } }, payload: FunctionalReadyPayload) => {
    if (event?.sender && typeof event.sender.id === 'number') {
      verdicts.set(event.sender.id, payload)
    }
  })

  return {
    get: (webContentsId: number) => verdicts.get(webContentsId) ?? null,
    // A reload re-runs boot: its old verdict no longer describes the window.
    forget: (webContentsId: number) => void verdicts.delete(webContentsId)
  }
}

// Runs one reopen at a time: a request while one is active joins it instead of
// starting a second, overlapping queue (Codex A4 #4).
export function createSingleFlight<T>(run: () => Promise<T>) {
  let active: Promise<T> | null = null

  return () => {
    if (!active) {
      active = run().finally(() => {
        active = null
      })
    }

    return active
  }
}

export function waitForSessionWindowFunctionallyReady(
  win: ReopenGateWindow,
  {
    profile,
    sessionId,
    probeTarget
  }: { profile: string | null; sessionId?: null | string; probeTarget?: ReopenProbeTarget },
  deps: ReopenGateDeps
): Promise<ReopenGateResult> {
  if (win.isDestroyed()) {
    return Promise.resolve({ ok: true })
  }

  const { ipcMain, knownVerdict, probeBackendHealth, timeoutMs = REOPEN_WINDOW_READY_TIMEOUT_MS } = deps
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

      // The window must have come up on the session it was asked to open
      // (Codex A4 #3): a window that silently fell back to another session is
      // not a successful restore.
      if (sessionId !== undefined && (payload.sessionId ?? null) !== (sessionId ?? null)) {
        finish({
          ok: false,
          reason: `window reported ready on session "${payload.sessionId ?? 'none'}", expected "${sessionId ?? 'none'}"`
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

    if (knownVerdict) {
      onReady({ sender: { id: wcId } }, knownVerdict)
    }

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

    void probeBackendHealth(probeTarget ?? { profile, connectionId: null }, controller.signal)
      .then(() => {
        healthReady = true
        succeedIfBothReady()
      })
      .catch(err => {
        finish({ ok: false, reason: `backend health check failed: ${err?.message || err}` })
      })
  })
}
