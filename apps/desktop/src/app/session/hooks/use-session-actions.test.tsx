import { cleanup, render, waitFor } from '@testing-library/react'
import type { MutableRefObject } from 'react'
import { useEffect } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { getSession, getSessionMessages } from '@/hermes'
import { $activeGatewayProfile, $newChatProfile, ensureGatewayProfile } from '@/store/profile'
import {
  $currentCwd,
  $messages,
  $resumeFailedSessionId,
  setMessages,
  setResumeFailedSessionId,
  setSessions
} from '@/store/session'
import { secondaryWindowProfile } from '@/store/windows'

import type { ClientSessionState } from '../../types'

import { useSessionActions } from './use-session-actions'

vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  deleteSession: vi.fn(),
  getSession: vi.fn(),
  getSessionMessages: vi.fn(),
  listAllProfileSessions: vi.fn(),
  setApiRequestProfile: vi.fn(),
  setSessionArchived: vi.fn()
}))

vi.mock('@/store/profile', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  ensureGatewayProfile: vi.fn(async () => undefined)
}))

vi.mock('@/store/windows', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  secondaryWindowProfile: vi.fn(() => null)
}))

const RUNTIME_SESSION_ID = 'rt-new-001'

function Harness({
  onReady,
  requestGateway
}: {
  onReady: (create: (preview?: string | null) => Promise<string | null>) => void
  requestGateway: <T>(method: string, params?: Record<string, unknown>) => Promise<T>
}) {
  const ref = <T,>(value: T): MutableRefObject<T> => ({ current: value })

  const actions = useSessionActions({
    activeSessionId: null,
    activeSessionIdRef: ref<string | null>(null),
    busyRef: ref(false),
    creatingSessionRef: ref(false),
    ensureSessionState: () => ({}) as ClientSessionState,
    getRouteToken: () => 'token',
    navigate: vi.fn() as never,
    requestGateway,
    runtimeIdByStoredSessionIdRef: ref(new Map<string, string>()),
    selectedStoredSessionId: null,
    selectedStoredSessionIdRef: ref<string | null>(null),
    sessionStateByRuntimeIdRef: ref(new Map<string, ClientSessionState>()),
    syncSessionStateToView: vi.fn(),
    updateSessionState: () => ({}) as ClientSessionState
  })

  useEffect(() => {
    onReady(actions.createBackendSessionForSend)
  }, [actions.createBackendSessionForSend, onReady])

  return null
}

async function createWith(profileSetup: () => void): Promise<Record<string, unknown> | undefined> {
  let createParams: Record<string, unknown> | undefined

  const requestGateway = vi.fn(async (method: string, params?: Record<string, unknown>) => {
    if (method === 'session.create') {
      createParams = params

      return { session_id: RUNTIME_SESSION_ID, stored_session_id: null } as never
    }

    return {} as never
  })

  $currentCwd.set('')
  profileSetup()

  let create: ((preview?: string | null) => Promise<string | null>) | null = null
  render(<Harness onReady={c => (create = c)} requestGateway={requestGateway} />)
  await waitFor(() => expect(create).not.toBeNull())
  await create!()

  return createParams
}

describe('createBackendSessionForSend profile routing', () => {
  afterEach(() => {
    cleanup()
    $newChatProfile.set(null)
    $activeGatewayProfile.set('default')
    vi.restoreAllMocks()
  })

  it('routes a plain new chat (no explicit profile) to the live gateway profile', async () => {
    // The "rubberband to default" bug: the top New Session button clears
    // $newChatProfile to null. In global-remote mode one backend serves every
    // profile, so an omitted `profile` lands the chat on the launch (default)
    // profile. The session must instead carry the active gateway profile.
    const params = await createWith(() => {
      $activeGatewayProfile.set('coder')
      $newChatProfile.set(null)
    })

    expect(params).toMatchObject({ profile: 'coder' })
  })

  it('honours an explicit per-profile "+" selection', async () => {
    const params = await createWith(() => {
      $activeGatewayProfile.set('coder')
      $newChatProfile.set('analyst')
    })

    expect(params).toMatchObject({ profile: 'analyst' })
  })

  it('passes the default profile for single-profile users (backend resolves it to launch)', async () => {
    const params = await createWith(() => {
      $activeGatewayProfile.set('default')
      $newChatProfile.set(null)
    })

    expect(params).toMatchObject({ profile: 'default' })
  })
})

// ── Resume failure recovery (the "stuck loading session window" bug) ──────────
// When session.resume rejects AND the REST transcript fallback ALSO fails, the
// hook must (a) not throw out of the fallback (which stranded the loader), and
// (b) arm $resumeFailedSessionId so use-route-resume can retry. A resume that
// succeeds must NOT leave the flag armed.
function ResumeHarness({
  onReady,
  requestGateway
}: {
  onReady: (resume: (storedSessionId: string, replaceRoute?: boolean) => Promise<unknown>) => void
  requestGateway: <T>(method: string, params?: Record<string, unknown>) => Promise<T>
}) {
  const ref = <T,>(value: T): MutableRefObject<T> => ({ current: value })

  const actions = useSessionActions({
    activeSessionId: null,
    activeSessionIdRef: ref<string | null>(null),
    busyRef: ref(false),
    creatingSessionRef: ref(false),
    ensureSessionState: () => ({}) as ClientSessionState,
    getRouteToken: () => 'token',
    navigate: vi.fn() as never,
    requestGateway,
    runtimeIdByStoredSessionIdRef: ref(new Map<string, string>()),
    selectedStoredSessionId: null,
    selectedStoredSessionIdRef: ref<string | null>(null),
    sessionStateByRuntimeIdRef: ref(new Map<string, ClientSessionState>()),
    syncSessionStateToView: vi.fn(),
    updateSessionState: (_sessionId, updater) => updater({} as ClientSessionState)
  })

  useEffect(() => {
    onReady(actions.resumeSession)
  }, [actions.resumeSession, onReady])

  return null
}

describe('resumeSession failure recovery', () => {
  afterEach(() => {
    cleanup()
    setResumeFailedSessionId(null)
    setMessages([])
    setSessions([])
    $activeGatewayProfile.set('default')
    vi.mocked(secondaryWindowProfile).mockReturnValue(null)
    vi.mocked(getSession).mockReset()
    vi.mocked(getSessionMessages).mockReset()
    vi.mocked(ensureGatewayProfile).mockClear()
    vi.restoreAllMocks()
  })

  async function runResume(
    requestGateway: <T>(method: string, params?: Record<string, unknown>) => Promise<T>
  ): Promise<void> {
    let resume: ((storedSessionId: string, replaceRoute?: boolean) => Promise<unknown>) | null = null
    render(<ResumeHarness onReady={r => (resume = r)} requestGateway={requestGateway} />)
    await waitFor(() => expect(resume).not.toBeNull())
    await resume!('stored-1', true)
  }

  it('arms $resumeFailedSessionId when resume RPC and REST fallback both fail', async () => {
    // session.resume rejects (e.g. timeout against a wedged backend)...
    const requestGateway = vi.fn(async (method: string) => {
      if (method === 'session.resume') {
        throw new Error('request timed out: session.resume')
      }

      return {} as never
    })

    // ...and the REST transcript fallback also rejects (backend unreachable).
    vi.mocked(getSessionMessages).mockRejectedValue(new Error('network down'))

    await runResume(requestGateway)

    // The window is no longer silently stranded: the failure latch is armed for
    // the stored session, which use-route-resume consumes to retry.
    expect($resumeFailedSessionId.get()).toBe('stored-1')
  })

  it('does NOT arm the failure latch when the resume RPC fails but the REST fallback paints history', async () => {
    // session.resume rejects, but the REST transcript fallback succeeds and
    // hydrates a readable transcript — the window is NOT stranded.
    const requestGateway = vi.fn(async (method: string) => {
      if (method === 'session.resume') {
        throw new Error('request timed out: session.resume')
      }

      return {} as never
    })

    vi.mocked(getSessionMessages).mockResolvedValue({
      messages: [
        { content: 'hello', role: 'user', timestamp: 1 },
        { content: 'hi there', role: 'assistant', timestamp: 2 }
      ],
      session_id: 'stored-1'
    } as never)

    await runResume(requestGateway)

    // Arming here would auto-retry a window that already shows history and,
    // on exhaustion, blank that transcript behind the error overlay — a
    // regression vs. plain fallback-success. The latch must stay clear.
    expect($resumeFailedSessionId.get()).toBeNull()
    // The fallback transcript is visible.
    expect($messages.get().length).toBeGreaterThan(0)
  })

  it('does NOT throw out of the fallback when REST also fails (no unhandled rejection)', async () => {
    const requestGateway = vi.fn(async (method: string) => {
      if (method === 'session.resume') {
        throw new Error('request timed out: session.resume')
      }

      return {} as never
    })

    vi.mocked(getSessionMessages).mockRejectedValue(new Error('network down'))

    // resumeSession must resolve (swallow the fallback failure), not reject.
    await expect(runResume(requestGateway)).resolves.toBeUndefined()
  })

  it('leaves the failure latch clear when resume succeeds', async () => {
    // Pre-arm to prove a successful resume clears it (entry-clear path).
    setResumeFailedSessionId('stored-1')

    const requestGateway = vi.fn(async (method: string, params?: Record<string, unknown>) => {
      if (method === 'session.resume') {
        return { session_id: 'runtime-1', resumed: params?.session_id, messages: [], info: {} } as never
      }

      return {} as never
    })

    vi.mocked(getSessionMessages).mockResolvedValue({ messages: [] } as never)

    await runResume(requestGateway)

    expect($resumeFailedSessionId.get()).toBeNull()
  })

  it('uses the secondary window URL profile for the initial stored-session lookup and resume', async () => {
    vi.mocked(secondaryWindowProfile).mockReturnValue('wikireader')
    vi.mocked(getSession).mockResolvedValue({ id: 'stored-1', profile: 'wikireader' } as never)
    vi.mocked(getSessionMessages).mockResolvedValue({ messages: [] } as never)

    let resumeParams: Record<string, unknown> | undefined

    const requestGateway = vi.fn(async (method: string, params?: Record<string, unknown>) => {
      if (method === 'session.resume') {
        resumeParams = params

        return { session_id: 'runtime-1', messages: [], info: {} } as never
      }

      return {} as never
    })

    await runResume(requestGateway)

    expect(getSession).toHaveBeenCalledWith('stored-1', 'wikireader')
    expect(ensureGatewayProfile).toHaveBeenCalledWith('wikireader')
    expect(getSessionMessages).toHaveBeenCalledWith('stored-1', 'wikireader')
    expect(resumeParams).toMatchObject({ session_id: 'stored-1', profile: 'wikireader' })
  })

  it('resumes via the gateway default (deferred build) — not lazy, no eager opt-out', async () => {
    // The switch-latency fix lives backend-side: a normal cold resume gets the
    // gateway's default DEFERRED build (transcript returns immediately, agent
    // pre-warms in the background). The client must NOT force the synchronous
    // path (eager_build) and is only `lazy` for subagent watch windows.
    let resumeParams: Record<string, unknown> | undefined

    const requestGateway = vi.fn(async (method: string, params?: Record<string, unknown>) => {
      if (method === 'session.resume') {
        resumeParams = params

        return { session_id: 'runtime-1', resumed: params?.session_id, messages: [], info: {} } as never
      }

      return {} as never
    })

    vi.mocked(getSessionMessages).mockResolvedValue({ messages: [] } as never)

    await runResume(requestGateway)

    expect(resumeParams).not.toHaveProperty('lazy')
    expect(resumeParams).not.toHaveProperty('eager_build')
  })
})

// ── Task 8: route resume on a per-session backend ────────────────────────────
// A session-backend pop-out must open the STORED transcript from its URL and
// must not create a new session or resume a wrong/stale runtime id — even with a
// bogus active id or a stale stored→runtime cache left by a prior backend.
function task8CachedState(overrides: Partial<ClientSessionState> = {}): ClientSessionState {
  return {
    storedSessionId: 'stored-popout-1',
    messages: [],
    branch: '',
    cwd: '',
    model: 'x',
    provider: '',
    reasoningEffort: '',
    serviceTier: '',
    fast: false,
    yolo: false,
    personality: '',
    busy: false,
    awaitingResponse: false,
    streamId: null,
    sawAssistantPayload: false,
    pendingBranchGroup: null,
    interrupted: false,
    needsInput: false,
    turnStartedAt: null,
    ...overrides
  }
}

function Task8ResumeHarness({
  onReady,
  requestGateway,
  activeSessionId = null,
  runtimeMap = new Map<string, string>(),
  stateMap = new Map<string, ClientSessionState>()
}: {
  onReady: (resume: (storedSessionId: string, replaceRoute?: boolean) => Promise<unknown>) => void
  requestGateway: <T>(method: string, params?: Record<string, unknown>) => Promise<T>
  activeSessionId?: string | null
  runtimeMap?: Map<string, string>
  stateMap?: Map<string, ClientSessionState>
}) {
  const ref = <T,>(value: T): MutableRefObject<T> => ({ current: value })

  const actions = useSessionActions({
    activeSessionId,
    activeSessionIdRef: ref<string | null>(activeSessionId),
    busyRef: ref(false),
    creatingSessionRef: ref(false),
    ensureSessionState: () => ({}) as ClientSessionState,
    getRouteToken: () => 'token',
    navigate: vi.fn() as never,
    requestGateway,
    runtimeIdByStoredSessionIdRef: ref(runtimeMap),
    selectedStoredSessionId: null,
    selectedStoredSessionIdRef: ref<string | null>(null),
    sessionStateByRuntimeIdRef: ref(stateMap),
    syncSessionStateToView: vi.fn(),
    updateSessionState: (_sessionId, updater) => updater({} as ClientSessionState)
  })

  useEffect(() => {
    onReady(actions.resumeSession)
  }, [actions.resumeSession, onReady])

  return null
}

describe('resumeSession on a per-session backend (Task 8)', () => {
  afterEach(() => {
    cleanup()
    setResumeFailedSessionId(null)
    setMessages([])
    setSessions([])
    $activeGatewayProfile.set('default')
    vi.mocked(secondaryWindowProfile).mockReturnValue(null)
    vi.mocked(getSession).mockReset()
    vi.mocked(getSessionMessages).mockReset()
    vi.mocked(ensureGatewayProfile).mockClear()
    vi.restoreAllMocks()
  })

  it('cold pop-out resume uses the STORED id (no session.create) despite a bogus active id + stale cache', async () => {
    vi.mocked(secondaryWindowProfile).mockReturnValue('wikireader')
    vi.mocked(getSession).mockResolvedValue({ id: 'stored-popout-1', profile: 'wikireader' } as never)
    vi.mocked(getSessionMessages).mockResolvedValue({ messages: [] } as never)

    const calls: Array<{ method: string; params?: Record<string, unknown> }> = []
    const requestGateway = vi.fn(async (method: string, params?: Record<string, unknown>) => {
      calls.push({ method, params })

      if (method === 'session.resume') {
        return { session_id: 'runtime-fresh', messages: [], info: {} } as never
      }

      return {} as never
    })

    // Stale stored→runtime mapping WITHOUT a cached state → fast-path is skipped.
    const runtimeMap = new Map([['stored-popout-1', 'stale-runtime-999']])

    let resume: ((id: string, r?: boolean) => Promise<unknown>) | null = null
    render(
      <Task8ResumeHarness
        onReady={r => (resume = r)}
        requestGateway={requestGateway}
        activeSessionId="bogus-runtime-from-other-session"
        runtimeMap={runtimeMap}
      />
    )
    await waitFor(() => expect(resume).not.toBeNull())
    await resume!('stored-popout-1', true)

    const resumeCalls = calls.filter(call => call.method === 'session.resume')
    expect(resumeCalls).toHaveLength(1)
    expect(resumeCalls[0].params).toMatchObject({ session_id: 'stored-popout-1', profile: 'wikireader' })
    // Never a new session, never the bogus/stale runtime id.
    expect(calls.some(call => call.method === 'session.create')).toBe(false)
    expect(resumeCalls.every(call => call.params?.session_id === 'stored-popout-1')).toBe(true)
  })

  it('a stale cached runtime (post-restart 404) falls through to a full session.resume on the stored id', async () => {
    vi.mocked(secondaryWindowProfile).mockReturnValue('wikireader')
    vi.mocked(getSession).mockResolvedValue({ id: 'stored-popout-1', profile: 'wikireader' } as never)
    vi.mocked(getSessionMessages).mockResolvedValue({ messages: [] } as never)

    const calls: Array<{ method: string; params?: Record<string, unknown> }> = []
    const requestGateway = vi.fn(async (method: string, params?: Record<string, unknown>) => {
      calls.push({ method, params })

      if (method === 'session.usage') {
        // Cached runtime id was minted by a prior backend instance → 404.
        throw new Error('session not found')
      }

      if (method === 'session.resume') {
        return { session_id: 'runtime-fresh', messages: [], info: {} } as never
      }

      return {} as never
    })

    // Cached mapping WITH a state → the fast-path fires, hits session.usage, 404s,
    // then must fall through to a full resume on the STORED id. The cached view
    // state carries the string cwd/branch the fast-path paints before it 404s.
    const runtimeMap = new Map([['stored-popout-1', 'stale-runtime-999']])
    const stateMap = new Map([['stale-runtime-999', task8CachedState()]])

    let resume: ((id: string, r?: boolean) => Promise<unknown>) | null = null
    render(
      <Task8ResumeHarness onReady={r => (resume = r)} requestGateway={requestGateway} runtimeMap={runtimeMap} stateMap={stateMap} />
    )
    await waitFor(() => expect(resume).not.toBeNull())
    await resume!('stored-popout-1', true)

    expect(calls.some(call => call.method === 'session.usage' && call.params?.session_id === 'stale-runtime-999')).toBe(true)
    const resumeCalls = calls.filter(call => call.method === 'session.resume')
    expect(resumeCalls).toHaveLength(1)
    expect(resumeCalls[0].params).toMatchObject({ session_id: 'stored-popout-1' })
    expect(calls.some(call => call.method === 'session.create')).toBe(false)
  })
})
