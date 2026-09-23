import { useStore } from '@nanostores/react'
import { useEffect, useState } from 'react'

import { type BootFunctionalState, createReopenReadySignaller } from '@/lib/reopen-ready'
import { windowSessionReport } from '@/lib/window-session-report'
import { $desktopBoot } from '@/store/boot'
import { $activeConnectionId } from '@/store/connections'
import { $activeGatewayProfile } from '@/store/profile'
import { $activeSessionId, $resumeExhaustedSessionId, $selectedStoredSessionId } from '@/store/session'

import { resolveSessionOwner } from '../../session/hooks/use-session-actions/utils'

// completeDesktopBoot() runs only after every required boot operation (connect,
// profile adoption, the mandatory config fetch) succeeded; failDesktopBoot()
// marks a terminal failure (a retryable one goes back to running).
export function bootFunctionalState(boot: { error?: null | string; phase?: string }): BootFunctionalState {
  if (boot.phase === 'renderer.ready') {
    return 'ready'
  }

  if (boot.phase === 'renderer.error') {
    return { error: boot.error || 'boot failed' }
  }

  return 'pending'
}

// Tells main whether this window is functionally ready for the saved-window
// reopen queue (see lib/reopen-ready.ts), for the session it shows NOW, and
// re-tells it whenever that changes (a new session via /new, a route change, a
// boot retry). Harmless for windows the queue is not waiting on (main records
// every window's latest verdict).
//
// Each READY/failed report carries the session's fully resolved OWNER route
// (profile AND connection), resolved here BEFORE sending (Codex A4 round 5), so
// main never pairs it with a separately-arriving route report. Any change of
// what the window is establishing sends "pending" synchronously FIRST, before
// that resolution starts (round 6 #2) — see createReopenReadySignaller.
export function useReopenReadySignal(routedSessionId: null | string) {
  const [signaller] = useState(() =>
    createReopenReadySignaller(payload => window.hermesDesktop?.signalSessionFunctionalReady?.(payload))
  )

  const boot = useStore($desktopBoot)
  const selected = useStore($selectedStoredSessionId)
  const runtime = useStore($activeSessionId)
  const exhausted = useStore($resumeExhaustedSessionId)
  const profile = useStore($activeGatewayProfile)
  const connectionId = useStore($activeConnectionId)

  useEffect(() => {
    const target = routedSessionId ?? null
    const ambient = { profile: profile || null, connectionId }

    // Synchronous: sends pending on any establishment change.
    const step = signaller.update({ boot: bootFunctionalState(boot), target, selected, runtime, exhausted, ambient })

    if (!step) {
      return
    }

    let current = true

    void resolveSessionOwner(target)
      .catch(() => undefined)
      .then(owner => {
        if (current) {
          signaller.settle(step, windowSessionReport(target, owner, ambient))
        }
      })

    return () => {
      current = false
    }
  }, [boot, connectionId, exhausted, profile, routedSessionId, runtime, selected, signaller])
}
