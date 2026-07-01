import { describe, expect, it } from 'vitest'

import chatSource from './index.tsx?raw'

function chatHeaderMenuSnippet(): string {
  const source = chatSource.replace(/\r\n/g, '\n')
  const start = source.indexOf('<SessionActionsMenu')
  const end = source.indexOf('</SessionActionsMenu>', start)

  expect(start).toBeGreaterThanOrEqual(0)
  expect(end).toBeGreaterThan(start)

  return source.slice(start, end)
}

describe('ChatHeader pop-out routing', () => {
  it('uses only a persisted stored session id plus profile for top-bar pop-outs', () => {
    const snippet = chatHeaderMenuSnippet()

    expect(snippet).toContain("sessionId={selectedSessionId || ''}")
    expect(snippet).toContain('profile={activeStoredSession?.profile}')
    expect(snippet).not.toContain("sessionId={selectedSessionId || activeSessionId || ''}")
  })
})
