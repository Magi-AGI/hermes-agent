import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { canOpenSessionWindow, openNewSessionInNewWindow, openSessionInNewWindow } from './windows'

const desktopWindow = window as unknown as { hermesDesktop?: Window['hermesDesktop'] }
const initialHermesDesktop = desktopWindow.hermesDesktop

const notifyError = vi.fn()

vi.mock('./notifications', () => ({
  notifyError: (...args: unknown[]) => notifyError(...args)
}))

function installBridge(
  openSessionWindow?: Window['hermesDesktop']['openSessionWindow'],
  openNewSessionWindow?: Window['hermesDesktop']['openNewSessionWindow']
) {
  desktopWindow.hermesDesktop = {
    ...(openSessionWindow ? { openSessionWindow } : {}),
    ...(openNewSessionWindow ? { openNewSessionWindow } : {})
  } as unknown as Window['hermesDesktop']
}

beforeEach(() => {
  notifyError.mockClear()
})

afterEach(() => {
  if (initialHermesDesktop) {
    desktopWindow.hermesDesktop = initialHermesDesktop
  } else {
    delete desktopWindow.hermesDesktop
  }
})

describe('canOpenSessionWindow', () => {
  it('is false when the desktop bridge is absent', () => {
    delete desktopWindow.hermesDesktop
    expect(canOpenSessionWindow()).toBe(false)
  })

  it('is false when the bridge lacks openSessionWindow', () => {
    installBridge(undefined)
    expect(canOpenSessionWindow()).toBe(false)
  })

  it('is true when the bridge exposes openSessionWindow', () => {
    installBridge(vi.fn().mockResolvedValue({ ok: true }))
    expect(canOpenSessionWindow()).toBe(true)
  })
})

describe('openSessionInNewWindow', () => {
  it('no-ops without a session id', async () => {
    const open = vi.fn().mockResolvedValue({ ok: true })
    installBridge(open)

    await openSessionInNewWindow('')

    expect(open).not.toHaveBeenCalled()
    expect(notifyError).not.toHaveBeenCalled()
  })

  it('no-ops gracefully when the bridge is absent (web fallback)', async () => {
    delete desktopWindow.hermesDesktop

    await openSessionInNewWindow('s1')

    expect(notifyError).not.toHaveBeenCalled()
  })

  it('invokes the bridge with the session id', async () => {
    const open = vi.fn().mockResolvedValue({ ok: true })
    installBridge(open)

    await openSessionInNewWindow('s1')

    expect(open).toHaveBeenCalledWith('s1', undefined)
    expect(notifyError).not.toHaveBeenCalled()
  })

  it('forwards the watch flag for spectator (subagent) windows', async () => {
    const open = vi.fn().mockResolvedValue({ ok: true })
    installBridge(open)

    await openSessionInNewWindow('s1', { watch: true })

    expect(open).toHaveBeenCalledWith('s1', { watch: true })
    expect(notifyError).not.toHaveBeenCalled()
  })

  it('notifies on an ok:false result', async () => {
    installBridge(vi.fn().mockResolvedValue({ ok: false, error: 'invalid-session-id' }))

    await openSessionInNewWindow('s1')

    expect(notifyError).toHaveBeenCalledTimes(1)
  })

  it('notifies when the bridge throws', async () => {
    installBridge(vi.fn().mockRejectedValue(new Error('boom')))

    await openSessionInNewWindow('s1')

    expect(notifyError).toHaveBeenCalledTimes(1)
  })
})

describe('openNewSessionInNewWindow', () => {
  it('no-ops gracefully when the bridge is absent (web fallback)', async () => {
    delete desktopWindow.hermesDesktop

    await openNewSessionInNewWindow()

    expect(notifyError).not.toHaveBeenCalled()
  })

  it('no-ops when openNewSessionWindow is missing', async () => {
    installBridge(vi.fn().mockResolvedValue({ ok: true }))

    await openNewSessionInNewWindow()

    expect(notifyError).not.toHaveBeenCalled()
  })

  it('invokes the bridge', async () => {
    const openNew = vi.fn().mockResolvedValue({ ok: true })
    installBridge(vi.fn().mockResolvedValue({ ok: true }), openNew)

    await openNewSessionInNewWindow()

    expect(openNew).toHaveBeenCalledTimes(1)
    expect(notifyError).not.toHaveBeenCalled()
  })

  it('notifies on an ok:false result', async () => {
    installBridge(vi.fn().mockResolvedValue({ ok: true }), vi.fn().mockResolvedValue({ ok: false, error: 'nope' }))

    await openNewSessionInNewWindow()

    expect(notifyError).toHaveBeenCalledTimes(1)
  })
})

// The window-flag helpers cache their URL reads at module load, so each case
// stubs window.location and re-imports the module fresh (vi.resetModules) to get
// a clean cache.
describe('secondaryWindowBackendOptions (M4b)', () => {
  const originalLocation = window.location

  async function loadWithUrl(search: string, hash: string) {
    Object.defineProperty(window, 'location', {
      configurable: true,
      writable: true,
      value: { ...originalLocation, search, hash, href: `http://localhost/${search}${hash}` }
    })
    vi.resetModules()
    return import('./windows')
  }

  afterEach(() => {
    Object.defineProperty(window, 'location', { configurable: true, writable: true, value: originalLocation })
    vi.resetModules()
  })

  it('existing-session secondary window → { sessionId, isolation: auto }', async () => {
    const w = await loadWithUrl('?win=secondary&profile=claudetriad', '#/stored-session')
    expect(w.secondaryWindowSessionId()).toBe('stored-session')
    expect(w.secondaryWindowBackendOptions()).toEqual({ sessionId: 'stored-session', isolation: 'auto' })
  })

  it('new-session secondary window → undefined (new flag suppresses session routing)', async () => {
    const w = await loadWithUrl('?win=secondary&new=1', '#/stored-session')
    expect(w.secondaryWindowSessionId()).toBeNull()
    expect(w.secondaryWindowBackendOptions()).toBeUndefined()
  })

  it('primary / non-secondary window → undefined even with a route id', async () => {
    const w = await loadWithUrl('', '#/stored-session')
    expect(w.secondaryWindowBackendOptions()).toBeUndefined()
  })

  it('secondary window with no durable route id → undefined', async () => {
    const w = await loadWithUrl('?win=secondary', '#/')
    expect(w.secondaryWindowSessionId()).toBeNull()
    expect(w.secondaryWindowBackendOptions()).toBeUndefined()
  })
})
