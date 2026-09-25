// Post-ready liveness supervision for the local backends this app owns.
//
// Nothing watched a backend once it was ready. On 2026-09-07 a backend stopped answering while its
// process stayed alive (zero CPU, half-closed sockets piling up, then ECONNREFUSED); every window
// sat on "could not connect" until someone killed the exact PID by hand, after which the existing
// exit recovery respawned it in under a minute. This supervisor does that automatically, and first
// captures native dumps of the frozen tree so the next freeze leaves evidence (Lake's D7 policy,
// 2026-09-25: dump, then restart; in-flight turns on that backend are lost).
//
// "Unresponsive" is strict: every probe for `unresponsiveMs` failed (no HTTP response at all, not
// merely an error status) while the process stayed alive. A long GIL stall answers late but answers;
// a gap between probes longer than `maxGapMs` (machine asleep, this process stalled) restarts the
// window instead of counting as backend silence.

import http from 'node:http'

export const LIVENESS_INTERVAL_MS = 15_000
export const LIVENESS_PROBE_TIMEOUT_MS = 5_000
export const LIVENESS_UNRESPONSIVE_MS = 120_000
/** The dump helper's own timeout is 90 s; beyond this the check was interrupted, not completed. */
export const LIVENESS_CAPTURE_BUDGET_MS = 150_000

export interface LivenessTarget {
  pid: number
  /** `http://127.0.0.1:<port>` of the backend. */
  baseUrl: string
  /** Writes native dumps of the frozen process tree; resolves (never rejects) when done or given up. */
  captureDumps: () => Promise<void>
  /** Kills the exact process tree; the owner's exit handler and recovery take it from there. */
  kill: () => void
}

export interface LivenessWindow {
  firstFailureAt: null | number
  lastProbeAt: null | number
  failures: number
}

export const freshLivenessWindow = (): LivenessWindow => ({ failures: 0, firstFailureAt: null, lastProbeAt: null })

/** Fold one probe result into the window; `unresponsive` when silence has lasted the full budget. */
export function assessLiveness(
  window: LivenessWindow,
  answered: boolean,
  at: number,
  { maxGapMs, unresponsiveMs }: { maxGapMs: number; unresponsiveMs: number }
): { unresponsive: boolean; window: LivenessWindow } {
  if (answered) {
    return { unresponsive: false, window: { failures: 0, firstFailureAt: null, lastProbeAt: at } }
  }

  const gapped = window.lastProbeAt !== null && at - window.lastProbeAt > maxGapMs

  const next: LivenessWindow =
    gapped || window.firstFailureAt === null
      ? { failures: 1, firstFailureAt: at, lastProbeAt: at }
      : { failures: window.failures + 1, firstFailureAt: window.firstFailureAt, lastProbeAt: at }

  return {
    unresponsive: next.firstFailureAt !== null && at - next.firstFailureAt >= unresponsiveMs && next.failures >= 3,
    window: next
  }
}

/** True when the backend produced ANY HTTP response (status irrelevant: the loop is serving). */
export function probeAnswers(baseUrl: string, timeoutMs: number): Promise<boolean> {
  return new Promise(resolve => {
    const request = http.get(`${baseUrl}/api/health`, { agent: false, timeout: timeoutMs }, response => {
      response.resume()
      resolve(true)
    })

    request.on('timeout', () => {
      request.destroy()
      resolve(false)
    })
    request.on('error', () => resolve(false))
  })
}

export interface BackendLivenessOptions {
  log: (line: string) => void
  isAlive: (pid: number) => boolean
  probe?: (baseUrl: string, timeoutMs: number) => Promise<boolean>
  intervalMs?: number
  /** Longest a dump capture may take before the check counts as interrupted (asleep, stalled). */
  captureBudgetMs?: number
  probeTimeoutMs?: number
  unresponsiveMs?: number
  now?: () => number
}

export function createBackendLivenessSupervisor({
  captureBudgetMs = LIVENESS_CAPTURE_BUDGET_MS,
  intervalMs = LIVENESS_INTERVAL_MS,
  isAlive,
  log,
  now = Date.now,
  probe = probeAnswers,
  probeTimeoutMs = LIVENESS_PROBE_TIMEOUT_MS,
  unresponsiveMs = LIVENESS_UNRESPONSIVE_MS
}: BackendLivenessOptions) {
  const watched = new Map<
    string,
    { target: LivenessTarget; timer: null | ReturnType<typeof setTimeout>; window: LivenessWindow }
  >()

  const maxGapMs = intervalMs * 3 + probeTimeoutMs

  const schedule = (key: string, target: LivenessTarget) => {
    const entry = watched.get(key)

    if (!entry || entry.target !== target) {
      return
    }

    entry.timer = setTimeout(() => void tick(key, target), intervalMs)
    entry.timer.unref?.()
  }

  const tick = async (key: string, target: LivenessTarget) => {
    const answered = await probe(target.baseUrl, probeTimeoutMs)
    const entry = watched.get(key)

    // Unwatched or replaced while probing, or already gone: its exit path owns what happens next.
    if (!entry || entry.target !== target || !isAlive(target.pid)) {
      return
    }

    const verdict = assessLiveness(entry.window, answered, now(), { maxGapMs, unresponsiveMs })
    entry.window = verdict.window

    if (!verdict.unresponsive) {
      schedule(key, target)

      return
    }

    const silentForS = Math.round((now() - (verdict.window.firstFailureAt ?? now())) / 1000)
    log(`[liveness] ${key} backend pid ${target.pid} has not answered for ${silentForS}s while alive; capturing native dumps, then restarting it`)
    // The entry stays registered through the capture: unwatch() (an intentional teardown, quit,
    // or the process exiting) cancels the kill.
    const captureStartedAt = now()
    await target.captureDumps()

    if (watched.get(key)?.target !== target || !isAlive(target.pid)) {
      log(`[liveness] ${key} backend pid ${target.pid} was torn down or exited during the dump; not killing it`)

      return
    }

    // Revalidate before the kill: a backend that answers again recovered on its own, and a capture
    // that outlasted its budget means this process (or the machine) stalled, so start over.
    const recovered = await probe(target.baseUrl, probeTimeoutMs)
    const stalled = now() - captureStartedAt > captureBudgetMs

    if (watched.get(key)?.target !== target || !isAlive(target.pid)) {
      return
    }

    if (recovered || stalled) {
      log(`[liveness] ${key} backend pid ${target.pid} ${recovered ? 'answered again' : 'check was interrupted by a stall'}; not restarting it`)
      entry.window = freshLivenessWindow()
      schedule(key, target)

      return
    }

    watched.delete(key)
    target.kill()
  }

  /** Stop watching *key*; with *pid*, only when that exact backend is still the watched one. */
  const unwatch = (key: string, pid?: number) => {
    const entry = watched.get(key)

    if (entry && (pid === undefined || entry.target.pid === pid)) {
      if (entry.timer) {
        clearTimeout(entry.timer)
      }

      watched.delete(key)
    }
  }

  return {
    watch(key: string, target: LivenessTarget) {
      unwatch(key)
      watched.set(key, { target, timer: null, window: freshLivenessWindow() })
      schedule(key, target)
    },
    unwatch,
    /** Disarm whichever key watches *pid*: every deliberate stop of a backend calls this first. */
    unwatchPid(pid: number | undefined) {
      for (const [key, entry] of watched) {
        if (entry.target.pid === pid) {
          unwatch(key)
        }
      }
    },
    unwatchAll() {
      for (const key of [...watched.keys()]) {
        unwatch(key)
      }
    },
    watchedKeys: () => [...watched.keys()]
  }
}
