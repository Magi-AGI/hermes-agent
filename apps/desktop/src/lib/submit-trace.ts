/**
 * Unconditional prompt-submit trace (hardening queue item 1).
 *
 * A prompt that is lost before any RPC leaves no trace anywhere: the gateway
 * never sees it, and the phase0 diagnostics only fire on paths that reach
 * session resolution. Every submit therefore logs once at the composer's input
 * handler, BEFORE session resolution, and again at every exit — accepted, or
 * abandoned with a named reason. One trace id follows the submit from the
 * keypress through `prompt.submit`, so a missing prompt can be followed line by
 * line in desktop.log (renderer console capture, see electron/renderer-log.ts).
 *
 * Never logs prompt text — only char counts and ids.
 */

/** Console prefix the main-process capture lets through at any level. */
export const SUBMIT_TRACE_PREFIX = '[submit-trace] '

// Distinguishes windows in the shared desktop.log; the counter is per renderer.
const WINDOW_TAG = Math.random().toString(16).slice(2, 6).padEnd(4, '0')
let submitSeq = 0

export function nextSubmitTraceId(): string {
  submitSeq += 1

  return `${WINDOW_TAG}-${submitSeq}`
}

export function submitTrace(stage: string, data: Record<string, unknown>): void {
  try {
    console.info(`${SUBMIT_TRACE_PREFIX}${stage} ${JSON.stringify(data)}`)
  } catch {
    // Diagnostics must never break a send.
  }
}
