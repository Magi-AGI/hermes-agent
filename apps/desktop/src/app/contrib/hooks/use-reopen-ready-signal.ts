import { useStore } from '@nanostores/react'
import { useEffect, useState } from 'react'

import { type BootFunctionalState, createReopenReadySignaller } from '@/lib/reopen-ready'
import { $desktopBoot } from '@/store/boot'
import { $activeGatewayProfile } from '@/store/profile'
import { $activeSessionId, $resumeExhaustedSessionId, $selectedStoredSessionId } from '@/store/session'

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
export function useReopenReadySignal(routedSessionId: null | string) {
  const [signal] = useState(() =>
    createReopenReadySignaller(payload => window.hermesDesktop?.signalSessionFunctionalReady?.(payload))
  )

  const boot = useStore($desktopBoot)
  const selected = useStore($selectedStoredSessionId)
  const runtime = useStore($activeSessionId)
  const exhausted = useStore($resumeExhaustedSessionId)
  const profile = useStore($activeGatewayProfile)

  useEffect(() => {
    signal({ boot: bootFunctionalState(boot), target: routedSessionId ?? null, selected, runtime, exhausted, profile })
  }, [boot, exhausted, profile, routedSessionId, runtime, selected, signal])
}
