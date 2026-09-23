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
  // The window's establishment attempt this report belongs to (Codex A4
  // round 4 #1); see createReopenReadySignaller.
  attempt?: number
  // The session's resolved OWNER route, resolved by the renderer BEFORE it
  // sends (Codex A4 round 5): route and readiness always arrive together.
  connectionId?: string | null
  // The webContents LOAD generation, stamped by MAIN when it records the
  // report (createWindowReadinessTracker) — never chosen by the renderer.
  generation?: number
  // The window is (re)establishing its current target — a new session after
  // /new, a boot retry. Not a verdict: the gate keeps waiting.
  pending?: boolean
  phase?: string
  profile?: string | null
  sessionId?: string | null
  error?: string
}

export interface ReopenProbeTarget {
  connectionId: null | string
  profile: null | string
}

// The saved OPENING route of one saved window: the route its launch probe
// checks before the window has reported (see waitForSessionWindowFunctionally-
// Ready — once it reports, the gate probes exactly the route it reported). A
// peer's route recorded when it was spawned (exact connection + profile) when
// available, else its saved route; a session pop-out boots by profile (its URL
// carries no connection), so it is probed on that profile's route.
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

// How the queue treats the window a saved entry resolved to (Codex A4 round 3
// #1). A window the queue CREATED must come up on the saved entry's exact
// session/profile, and if it fails it is torn down so a retry recreates it.
// A window that was ALREADY OPEN is the user's live window: its saved entry can
// lag what it shows now (e.g. just after /new), so it is judged only on being
// functional (its latest verdict for its CURRENT target + its route probe), and
// on failure the queue halts WITHOUT closing it.
export type ReopenIdentityCheck = 'exact' | 'functional'

export function reopenedWindowPolicy(created: boolean): {
  destroyOnFailure: boolean
  identity: ReopenIdentityCheck
} {
  return created ? { identity: 'exact', destroyOnFailure: true } : { identity: 'functional', destroyOnFailure: false }
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
  // Main's per-webContents load generation, and reload notifications
  // (createWindowReadinessTracker; Codex A4 round 5).
  readiness?: ReopenReadinessSource
}

export interface ReopenReadinessSource {
  generation: (webContentsId: number) => number
  onReload: (webContentsId: number, listener: () => void) => () => void
}

// Every window's latest functional-ready verdict, recorded for the life of the
// app (Codex A4 #4). The reopen queue used to trust `created: false` (a window
// it only focused) as ready, which is wrong for a window that is still booting
// or already failed. A window re-reports whenever its verdict changes — a new
// session, route or boot attempt (useReopenReadySignal; Codex A4 round 3 #1) —
// so the entry here is always its LATEST state; one that has not reported yet
// (or is pending) is waited for like a new window.
//
// Main owns each webContents' LOAD generation (Codex A4 round 5): bumped on
// every (re)load or renderer crash (reload()), stamped onto each recorded
// report, never chosen by the renderer — a reloaded renderer restarts its
// attempt numbering, so attempt alone cannot tell two loads apart. A reload
// also tells every active gate on that window to drop its readiness and probe.
export function createWindowReadinessTracker(ipcMain: ReopenGateIpcMain) {
  const verdicts = new Map<number, FunctionalReadyPayload>()
  const generations = new Map<number, number>()
  const reloadListeners = new Map<number, Set<() => void>>()
  const generation = (webContentsId: number) => generations.get(webContentsId) ?? 0

  ipcMain.on(FUNCTIONAL_READY_CHANNEL, (event: { sender: { id: number } }, payload: FunctionalReadyPayload) => {
    if (event?.sender && typeof event.sender.id === 'number') {
      verdicts.set(event.sender.id, { ...payload, generation: generation(event.sender.id) })
    }
  })

  return {
    get: (webContentsId: number) => verdicts.get(webContentsId) ?? null,
    generation,
    // A (re)load or renderer crash re-runs boot: a new generation, and the old
    // verdict no longer describes the window.
    reload: (webContentsId: number) => {
      generations.set(webContentsId, generation(webContentsId) + 1)
      verdicts.delete(webContentsId)

      for (const listener of reloadListeners.get(webContentsId) ?? []) {
        listener()
      }
    },
    onReload: (webContentsId: number, listener: () => void) => {
      const listeners = reloadListeners.get(webContentsId) ?? new Set()

      listeners.add(listener)
      reloadListeners.set(webContentsId, listeners)

      return () => void listeners.delete(listener)
    },
    // The window is gone for good.
    forget: (webContentsId: number) => {
      verdicts.delete(webContentsId)
      generations.delete(webContentsId)
      reloadListeners.delete(webContentsId)
    }
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
    probeTarget,
    identity = 'exact'
  }: {
    identity?: ReopenIdentityCheck
    // The saved OPENING route: probed only until the window reports.
    probeTarget?: ReopenProbeTarget
    profile: string | null
    sessionId?: null | string
  },
  deps: ReopenGateDeps
): Promise<ReopenGateResult> {
  if (win.isDestroyed()) {
    return Promise.resolve({ ok: true })
  }

  const { ipcMain, knownVerdict, probeBackendHealth, readiness, timeoutMs = REOPEN_WINDOW_READY_TIMEOUT_MS } = deps
  const wcId = win.webContents.id
  const currentGeneration = () => readiness?.generation(wcId) ?? 0

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
    //
    // And both must belong to the window's LATEST identity (Codex A4 rounds 4
    // and 5): the tuple
    //
    //   (generation, attempt, sessionId, profile, connectionId)
    //
    // — main's load generation plus what ONE report carries (the renderer
    // resolves the session's owner route before sending, so route and
    // readiness never arrive separately). The probe checks exactly that
    // identity's route, and its result satisfies only the identity it was
    // started for. Any change of any field — a pending report, a new session,
    // profile or connection, a new attempt, a reload — clears renderer
    // readiness and aborts/ignores the in-flight probe. The launch probe (on
    // the saved opening route, run before any report) is reused only when the
    // first report's generation, session and route match it exactly.
    let rendererReady = false
    let healthReady = false
    let latestIdentity: null | string = null
    let probeRun = 0
    let probeIdentity: null | string = null
    let probeController: AbortController | null = null
    let reloads = 0

    const normalizeProfile = (p: string | null | undefined) => p || 'default'
    const LAUNCH = '\u0000launch'
    const launchRoute: ReopenProbeTarget = probeTarget ?? { profile, connectionId: null }
    const launchGeneration = currentGeneration()

    const routeKey = (generation: number, session: null | string | undefined, route: ReopenProbeTarget) =>
      JSON.stringify([generation, session ?? null, normalizeProfile(route.profile), route.connectionId ?? null])

    const launchKey = routeKey(launchGeneration, sessionId, launchRoute)

    const finish = (result: ReopenGateResult) => {
      if (settled) {
        return
      }

      settled = true
      probeController?.abort()
      clearTimeout(timer)
      ipcMain.removeListener(FUNCTIONAL_READY_CHANNEL, onReady)
      stopReloadWatch?.()
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
    const expectedProfile = normalizeProfile(profile)

    const dropProbe = () => {
      probeRun += 1
      probeIdentity = null
      probeController?.abort()
      probeController = null
      healthReady = false
    }

    const runProbe = (target: ReopenProbeTarget, forIdentity: string) => {
      dropProbe()

      const run = probeRun
      const controller = new AbortController()

      probeController = controller
      probeIdentity = forIdentity

      void probeBackendHealth(target, controller.signal)
        .then(() => {
          // Satisfies only the identity it was started for: every identity
          // change supersedes this run (runProbe/dropProbe bump probeRun),
          // except the launch probe's exact-match reuse for the first report.
          if (run === probeRun) {
            healthReady = true
            succeedIfBothReady()
          }
        })
        .catch(err => {
          // A probe superseded by a newer identity is discarded, not a failure.
          if (run === probeRun) {
            finish({ ok: false, reason: `backend health check failed: ${err?.message || err}` })
          }
        })
    }

    const onReady = (event: { sender: { id: number } }, payload: FunctionalReadyPayload) => {
      if (event.sender.id !== wcId) {
        return
      }

      // A recorded verdict carries the generation main stamped on it; a live
      // report belongs to the window's current load.
      const generation = typeof payload?.generation === 'number' ? payload.generation : currentGeneration()

      const route: ReopenProbeTarget = {
        profile: payload?.profile || profile,
        connectionId: payload?.connectionId ?? null
      }

      // A pending report carries NO route (round 6 #2): its identity is marked
      // as such, never filled in with a fallback route.
      const isPending = payload?.pending === true

      const nextIdentity = JSON.stringify([
        generation,
        payload?.attempt ?? null,
        payload?.sessionId ?? null,
        isPending ? '\u0000pending' : [normalizeProfile(route.profile), route.connectionId]
      ])

      if (nextIdentity !== latestIdentity) {
        rendererReady = false
        latestIdentity = nextIdentity

        if (isPending) {
          // Never authorizes a probe (there is no resolved route to probe); a
          // probe bound to an earlier report no longer counts. Only the launch
          // probe (saved opening route, bound to no report) survives, for the
          // first routed report to claim.
          if (probeIdentity !== LAUNCH) {
            dropProbe()
          }
        } else if (probeIdentity === LAUNCH && routeKey(generation, payload?.sessionId, route) === launchKey) {
          // The first routed report matches the launch probe's identity
          // exactly: its result counts for it.
          probeIdentity = nextIdentity
        } else {
          runProbe(route, nextIdentity)
        }
      }

      if (payload?.pending === true) {
        rendererReady = false

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

      if (identity === 'exact' && reportedProfile !== expectedProfile) {
        finish({
          ok: false,
          reason: `window reported ready for profile "${reportedProfile}", expected "${expectedProfile}"`
        })

        return
      }

      // The window must have come up on the session it was asked to open
      // (Codex A4 #3): a window that silently fell back to another session is
      // not a successful restore.
      if (identity === 'exact' && sessionId !== undefined && (payload.sessionId ?? null) !== (sessionId ?? null)) {
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

    // A reload invalidates this window's readiness and its probe; the next
    // load's first report is a new identity (its generation differs) and is
    // probed afresh.
    const stopReloadWatch = readiness?.onReload(wcId, () => {
      rendererReady = false
      dropProbe()
      reloads += 1
      latestIdentity = `\u0000reload:${reloads}`
    })

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

    // Before any report: probe the saved opening route (reused only if the
    // first report matches it — see above).
    if (!settled && latestIdentity === null) {
      runProbe(launchRoute, LAUNCH)
    }
  })
}
