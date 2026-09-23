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
