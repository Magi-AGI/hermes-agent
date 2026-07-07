import { describe, expect, it, vi } from 'vitest'

import { GatewayReauthRequiredError, isGatewayReauthRequired, resolveGatewayWsUrl } from './gateway-ws-url'

const oauthConn = { authMode: 'oauth' as const, wsUrl: 'ws://host/api/ws?ticket=stale' }
const tokenConn = { authMode: 'token' as const, wsUrl: 'ws://host/api/ws?token=abc' }

describe('resolveGatewayWsUrl', () => {
  describe('oauth mode', () => {
    it('uses the freshly minted URL', async () => {
      const getGatewayWsUrl = vi.fn().mockResolvedValue('ws://host/api/ws?ticket=fresh')
      await expect(resolveGatewayWsUrl({ getGatewayWsUrl }, oauthConn)).resolves.toBe('ws://host/api/ws?ticket=fresh')
      expect(getGatewayWsUrl).toHaveBeenCalledOnce()
    })

    it('throws a reauth error instead of falling back to the stale cached ticket', async () => {
      const getGatewayWsUrl = vi.fn().mockRejectedValue(new Error('401 cookie expired'))
      await expect(resolveGatewayWsUrl({ getGatewayWsUrl }, oauthConn)).rejects.toBeInstanceOf(
        GatewayReauthRequiredError
      )
    })

    it('preserves the underlying mint failure as the cause', async () => {
      const cause = new Error('401 cookie expired')
      const getGatewayWsUrl = vi.fn().mockRejectedValue(cause)
      const error = await resolveGatewayWsUrl({ getGatewayWsUrl }, oauthConn).catch(e => e)
      expect(error).toBeInstanceOf(GatewayReauthRequiredError)
      expect((error as GatewayReauthRequiredError).cause).toBe(cause)
    })

    it('throws a reauth error when the preload cannot mint (no method)', async () => {
      await expect(resolveGatewayWsUrl({}, oauthConn)).rejects.toBeInstanceOf(GatewayReauthRequiredError)
    })

    it('never returns the stale cached ticket on failure', async () => {
      const getGatewayWsUrl = vi.fn().mockRejectedValue(new Error('boom'))
      const result = await resolveGatewayWsUrl({ getGatewayWsUrl }, oauthConn).catch(() => 'threw')
      expect(result).toBe('threw')
      expect(result).not.toBe(oauthConn.wsUrl)
    })
  })

  describe('token / local mode', () => {
    it('uses the minted URL when available', async () => {
      const getGatewayWsUrl = vi.fn().mockResolvedValue('ws://host/api/ws?token=fresh')
      await expect(resolveGatewayWsUrl({ getGatewayWsUrl }, tokenConn)).resolves.toBe('ws://host/api/ws?token=fresh')
    })

    it('falls back to the cached URL when minting fails (token is long-lived)', async () => {
      const getGatewayWsUrl = vi.fn().mockRejectedValue(new Error('transient'))
      await expect(resolveGatewayWsUrl({ getGatewayWsUrl }, tokenConn)).resolves.toBe(tokenConn.wsUrl)
    })

    it('falls back to the cached URL when the preload method is absent', async () => {
      await expect(resolveGatewayWsUrl({}, tokenConn)).resolves.toBe(tokenConn.wsUrl)
    })

    it('treats a missing authMode as non-oauth (falls back safely)', async () => {
      await expect(resolveGatewayWsUrl({}, { wsUrl: tokenConn.wsUrl })).resolves.toBe(tokenConn.wsUrl)
    })
  })

  describe('session backend options (M4b)', () => {
    const sessionOpts = { sessionId: 'stored-session', isolation: 'auto' as const }

    it('passes opts (and the conn profile) into the mint for OAuth', async () => {
      const getGatewayWsUrl = vi.fn().mockResolvedValue('ws://host/api/ws?ticket=fresh')
      await resolveGatewayWsUrl({ getGatewayWsUrl }, { ...oauthConn, profile: 'claudetriad' }, sessionOpts)
      expect(getGatewayWsUrl).toHaveBeenCalledWith('claudetriad', sessionOpts)
    })

    it('passes opts into the mint for the token/local re-mint path', async () => {
      const getGatewayWsUrl = vi.fn().mockResolvedValue('ws://host/api/ws?token=fresh')
      await resolveGatewayWsUrl({ getGatewayWsUrl }, { ...tokenConn, profile: 'claudetriad' }, sessionOpts)
      expect(getGatewayWsUrl).toHaveBeenCalledWith('claudetriad', sessionOpts)
    })

    it('still falls back to the cached URL when a token/local mint with opts fails', async () => {
      const getGatewayWsUrl = vi.fn().mockRejectedValue(new Error('transient'))
      await expect(resolveGatewayWsUrl({ getGatewayWsUrl }, tokenConn, sessionOpts)).resolves.toBe(tokenConn.wsUrl)
      expect(getGatewayWsUrl).toHaveBeenCalledWith(null, sessionOpts)
    })
  })
})

describe('isGatewayReauthRequired', () => {
  it('detects the dedicated error class', () => {
    expect(isGatewayReauthRequired(new GatewayReauthRequiredError('x'))).toBe(true)
  })

  it('detects plain objects tagged with needsOauthLogin (from the main process)', () => {
    expect(isGatewayReauthRequired({ needsOauthLogin: true })).toBe(true)
  })

  it('rejects generic errors', () => {
    expect(isGatewayReauthRequired(new Error('connection closed'))).toBe(false)
    expect(isGatewayReauthRequired(null)).toBe(false)
    expect(isGatewayReauthRequired('string')).toBe(false)
  })
})
