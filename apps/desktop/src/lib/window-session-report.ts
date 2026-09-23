// What a restorable window reports to main about the session it is showing
// (hermes:window:session), so its saved-layout entry reopens it on the SAME
// backend (Codex A4 #5). The session's resolved OWNER route travels with its id
// as one unit: a session owned by another connection/profile than the one the
// window happens to be on must reopen against its owner, and switching route
// updates both together.

export interface WindowRoute {
  connectionId: null | string
  profile: null | string
}

export interface WindowSessionReport extends WindowRoute {
  sessionId: null | string
}

// `owner` is resolveSessionOwner()'s answer: an exact route, a bare profile
// name, or undefined (unknown). Unknown, or no session (a fresh draft), falls
// back to the window's active route.
export function windowSessionReport(
  sessionId: null | string,
  owner: { connectionId?: null | string; profile?: null | string } | null | string | undefined,
  active: WindowRoute
): WindowSessionReport {
  if (!sessionId || !owner) {
    return { sessionId: sessionId || null, ...active }
  }

  // A bare profile belongs to the ambient backend that listed it (see
  // sessionOwnerRouteFromRow): the window's active connection.
  if (typeof owner === 'string') {
    return { sessionId, profile: owner, connectionId: active.connectionId }
  }

  return { sessionId, profile: owner.profile || active.profile, connectionId: owner.connectionId ?? null }
}

// A report is an authoritative route only once this window's boot has
// ESTABLISHED the route it was launched on (Codex A4 round 3 #4). Before that the
// renderer holds initial defaults (default profile, no connection); main keeps
// the launch route it recorded and ignores reports that are not established.
export function establishedWindowSessionReport(
  report: WindowSessionReport,
  bootPhase: string | undefined
): WindowSessionReport & { established: boolean } {
  return { ...report, established: bootPhase === 'renderer.ready' }
}

// Which windows report: every RESTORABLE kind — full peer windows and session
// pop-outs (a pop-out can move to another session, e.g. via /new; Codex A4 #6).
// Main ignores windows it does not save (watch windows).
export function reportsWindowSession(kind: { peer: boolean; secondary: boolean }): boolean {
  return kind.peer || kind.secondary
}
