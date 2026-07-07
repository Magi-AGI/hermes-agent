import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ConfigSchemaResponse, HermesConfigRecord } from '@/types/hermes'

const getElevenLabsVoices = vi.fn()
const getHermesConfigDefaults = vi.fn()
const getHermesConfigRecord = vi.fn()
const getHermesConfigSchema = vi.fn()
const notify = vi.fn()
const reconcileBackends = vi.fn()
const saveHermesConfig = vi.fn()

vi.mock('@/hermes', () => ({
  getElevenLabsVoices: () => getElevenLabsVoices(),
  getHermesConfigDefaults: () => getHermesConfigDefaults(),
  getHermesConfigRecord: () => getHermesConfigRecord(),
  getHermesConfigSchema: () => getHermesConfigSchema(),
  saveHermesConfig: (config: HermesConfigRecord) => saveHermesConfig(config)
}))

vi.mock('@/store/notifications', () => ({
  notify: (payload: unknown) => notify(payload),
  notifyError: vi.fn()
}))

beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn()
  Element.prototype.hasPointerCapture = vi.fn(() => false)
  Element.prototype.releasePointerCapture = vi.fn()
})

const desktopConfig: HermesConfigRecord = {
  desktop: {
    backend_isolation: 'profile',
    backend_pool: {
      auto_reap_orphans: true,
      health_timeout_ms: 2500,
      idle_ms: 600_000,
      max_profile_backends: 3,
      max_session_backends_per_profile: 10,
      startup_timeout_ms: 90_000
    }
  }
}

const desktopSchema: ConfigSchemaResponse = {
  fields: {
    'desktop.backend_isolation': { type: 'string', description: 'Desktop backend isolation mode' },
    'desktop.backend_pool.max_profile_backends': { type: 'number', description: 'Maximum profile backends' },
    'desktop.backend_pool.max_session_backends_per_profile': {
      type: 'number',
      description: 'Maximum session backends per profile'
    },
    'desktop.backend_pool.idle_ms': { type: 'number', description: 'Idle timeout in milliseconds' },
    'desktop.backend_pool.auto_reap_orphans': { type: 'boolean', description: 'Reap orphaned backend processes' }
  }
}

beforeEach(() => {
  getElevenLabsVoices.mockResolvedValue({ available: false, voices: [] })
  getHermesConfigDefaults.mockResolvedValue(desktopConfig)
  getHermesConfigRecord.mockResolvedValue(desktopConfig)
  getHermesConfigSchema.mockResolvedValue(desktopSchema)
  reconcileBackends.mockResolvedValue({ ok: true, reaped: 2, pruned: 1, tombstoned: 0, kept: 3 })
  saveHermesConfig.mockResolvedValue({ ok: true })
  ;(window as unknown as { hermesDesktop: { reconcileBackends: typeof reconcileBackends } }).hermesDesktop = {
    reconcileBackends
  }
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  vi.restoreAllMocks()
})

async function renderAdvancedConfigSettings() {
  const { ConfigSettings } = await import('./config-settings')

  return render(
    <MemoryRouter>
      <ConfigSettings activeSectionId="advanced" importInputRef={{ current: null }} />
    </MemoryRouter>
  )
}

describe('ConfigSettings desktop backend controls', () => {
  it('renders desktop backend isolation settings in the curated Advanced section', async () => {
    await renderAdvancedConfigSettings()

    expect(await screen.findByText('Backend isolation')).toBeTruthy()
    expect(screen.getByText('Max profile backends')).toBeTruthy()
    expect(screen.getByText('Max session backends per profile')).toBeTruthy()
    expect(screen.getByText('Backend idle timeout')).toBeTruthy()
    expect(screen.getByText('Auto-reap orphaned backends')).toBeTruthy()
    expect(screen.getAllByText(/Restart Desktop to apply/).length).toBeGreaterThan(0)
  })

  it('writes the canonical top-level isolation config path and not the legacy nested alias', async () => {
    await renderAdvancedConfigSettings()

    await screen.findByText('Backend isolation')
    fireEvent.click(screen.getAllByRole('combobox')[0])
    fireEvent.click(await screen.findByText('Hybrid'))

    await waitFor(() =>
      expect(saveHermesConfig).toHaveBeenCalledWith(
        expect.objectContaining({
          desktop: expect.objectContaining({
            backend_isolation: 'hybrid',
            backend_pool: expect.not.objectContaining({ backend_isolation: expect.anything() })
          })
        })
      )
    )
  })

  it('clamps backend pool counts to a safe positive integer before saving', async () => {
    await renderAdvancedConfigSettings()

    fireEvent.change(await screen.findByDisplayValue('3'), { target: { value: '-5' } })

    await waitFor(() =>
      expect(saveHermesConfig).toHaveBeenCalledWith(
        expect.objectContaining({
          desktop: expect.objectContaining({
            backend_pool: expect.objectContaining({ max_profile_backends: 1 })
          })
        })
      )
    )
  })

  it('offers a manual reap action for verified orphaned Desktop backends', async () => {
    await renderAdvancedConfigSettings()

    fireEvent.click(await screen.findByRole('button', { name: /Reap orphaned backends/i }))

    await waitFor(() => expect(reconcileBackends).toHaveBeenCalledTimes(1))
    expect(notify).toHaveBeenCalledWith(
      expect.objectContaining({
        kind: 'success',
        message: expect.stringContaining('reaped 2')
      })
    )
  })
})
