import { useStore } from '@nanostores/react'
import { useEffect, useRef } from 'react'

import { type BootFunctionalState, reopenReadyVerdict } from '@/lib/reopen-ready'
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

// Tells main when this window is functionally ready for the saved-window reopen
// queue (see lib/reopen-ready.ts). The target is the session in the window's
// LAUNCH route, captured once; later navigation does not change it. Sent once;
// harmless for windows the queue is not waiting on (main listens per window).
export function useReopenReadySignal(routedSessionId: null | string) {
  const targetRef = useRef<null | string | undefined>(undefined)

  if (targetRef.current === undefined) {
    targetRef.current = routedSessionId
  }

  const sentRef = useRef(false)
  const boot = useStore($desktopBoot)
  const selected = useStore($selectedStoredSessionId)
  const runtime = useStore($activeSessionId)
  const exhausted = useStore($resumeExhaustedSessionId)
  const profile = useStore($activeGatewayProfile)

  // sentRef is a one-time send latch, not a mirror of reactive atom state.
  // eslint-disable-next-line no-restricted-syntax
  useEffect(() => {
    if (sentRef.current) {
      return
    }

    const verdict = reopenReadyVerdict({
      boot: bootFunctionalState(boot),
      target: targetRef.current ?? null,
      selected,
      runtime,
      exhausted,
      profile
    })

    if (verdict) {
      sentRef.current = true
      window.hermesDesktop?.signalSessionFunctionalReady?.(verdict)
    }
  }, [boot, exhausted, profile, runtime, selected])
}
