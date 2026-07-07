import { cleanup, render, waitFor } from '@testing-library/react'
import { useEffect } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $gateway } from '@/store/gateway'
import { $activeGatewayProfile } from '@/store/profile'
import { $gatewayState } from '@/store/session'
import { secondaryWindowBackendOptions } from '@/store/windows'

import { useGatewayRequest } from './use-gateway-request'

// Keep the real $gateway atom (so gatewayRef's subscribe works), but force the
// active gateway to be the primary and stub the background-profile reconnect —
// a session pop-out's gateway IS the primary (M4b setPrimaryGateway).
vi.mock('@/store/gateway', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  isActivePrimary: vi.fn(() => true),
  ensureActiveGatewayOpen: vi.fn(async () => null)
}))

// The URL→options derivation is covered in windows.test.ts; here we only prove
// the hook THREADS whatever it returns into the reconnect calls. Spread the real
// module so other exports (used transitively) keep working.
vi.mock('@/store/windows', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  secondaryWindowBackendOptions: vi.fn(() => undefined)
}))

type RequestGateway = ReturnType<typeof useGatewayRequest>['requestGateway']

function Harness({ onReady }: { onReady: (rq: RequestGateway) => void }) {
  const { requestGateway } = useGatewayRequest()

  useEffect(() => {
    onReady(requestGateway)
  }, [onReady, requestGateway])

  return null
}

function fakeDesktop(profile: string) {
  const conn = {
    authMode: 'token' as const,
    baseUrl: `https://vps.example.com/${profile}`,
    profile,
    token: 't',
    wsUrl: `wss://vps.example.com/${profile}/api/ws?token=t`
  }

  return {
    getConnection: vi.fn(async () => conn),
    getGatewayWsUrl: vi.fn(async () => conn.wsUrl)
  }
}

// A gateway whose first request drops the socket and whose retry (after the
// recovery reconnect) succeeds.
function fakeGateway() {
  return {
    request: vi.fn().mockRejectedValueOnce(new Error('connection closed')).mockResolvedValueOnce({ ok: true }),
    connect: vi.fn(async () => undefined)
  }
}

async function runRecovery(): Promise<RequestGateway> {
  let requestGateway: RequestGateway | null = null
  render(<Harness onReady={rq => (requestGateway = rq)} />)
  await waitFor(() => expect(requestGateway).not.toBeNull())

  return requestGateway as unknown as RequestGateway
}

describe('useGatewayRequest transport recovery', () => {
  beforeEach(() => {
    // Non-open so ensureGatewayOpen actually reconnects instead of reusing.
    $gatewayState.set('idle')
  })

  afterEach(() => {
    cleanup()
    $gateway.set(null as never)
    $gatewayState.set('idle')
    $activeGatewayProfile.set('default')
    vi.mocked(secondaryWindowBackendOptions).mockReturnValue(undefined)
    delete (window as { hermesDesktop?: unknown }).hermesDesktop
    vi.clearAllMocks()
  })

  it('recovers an existing-session pop-out request against ITS session backend (with opts)', async () => {
    const opts = { sessionId: 'stored-session-1', isolation: 'auto' as const }
    vi.mocked(secondaryWindowBackendOptions).mockReturnValue(opts)
    $activeGatewayProfile.set('wikireader')

    const desktop = fakeDesktop('wikireader')
    ;(window as { hermesDesktop?: unknown }).hermesDesktop = desktop
    const gateway = fakeGateway()
    $gateway.set(gateway as never)

    const requestGateway = await runRecovery()
    const result = await requestGateway('session.resume', { session_id: 'stored-session-1' })

    // The reconnect + WS re-mint carry the SAME session backend options as M4b
    // boot/reconnect/keepalive — not a bare profile-backend connection.
    expect(desktop.getConnection).toHaveBeenCalledWith('wikireader', opts)
    expect(desktop.getGatewayWsUrl).toHaveBeenCalledWith('wikireader', opts)
    // First request dropped, retry (post-recovery) succeeded.
    expect(gateway.request).toHaveBeenCalledTimes(2)
    expect(result).toEqual({ ok: true })
  })

  it('primary / non-secondary window recovery passes NO backend options (unchanged)', async () => {
    vi.mocked(secondaryWindowBackendOptions).mockReturnValue(undefined)
    $activeGatewayProfile.set('default')

    const desktop = fakeDesktop('default')
    ;(window as { hermesDesktop?: unknown }).hermesDesktop = desktop
    const gateway = fakeGateway()
    $gateway.set(gateway as never)

    const requestGateway = await runRecovery()
    await requestGateway('session.list', {})

    expect(desktop.getConnection).toHaveBeenCalledWith('default', undefined)
    expect(desktop.getGatewayWsUrl).toHaveBeenCalledWith('default', undefined)
    expect(gateway.request).toHaveBeenCalledTimes(2)
  })
})
