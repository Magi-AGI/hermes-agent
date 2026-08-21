# Triad Review — Bucket B (desktop session windows) — 2026-08-20

**For:** Codex (authoritative) + Antigravity. **Branch:** `magi/reconcile-2026-08-20`. Committed as **B4 `b425528603`** (checkpoint — review against `git show b425528603` / `git diff 67b0557999 b425528603`). Follow-up commits fine if you find issues.

## What was done
Re-expressed our desktop overlay onto upstream `b44871db0a`. KEEP features (all confirmed absent upstream): durable window-set (`SESSION_WINDOWS_STATE_PATH`), gateway-gated reopen queue (`waitForSessionWindowGatewayReady`), empty-capture voice guard (`noAudioCaptured`). DROP per D-1: our rename plumbing — adopted upstream's rename UI, retained ONLY the `disableRename` gate.

## Conflict resolutions (please verify)
- **gateway.ts (HOT, your key concern):** applied clean; emitter verified — `signalSessionGatewayReady()` sits inside `reportPrimaryGatewayState`'s `state === 'open'` block (line ~234), the TRUE primary-ready path, NOT `openSecondary`/upstream's secondary-*gateway* map.
- **main.ts (HOT):** 2 conflicts. (1) keep-both const (`DESKTOP_BACKEND_OWNERSHIP_PATH` + `SESSION_WINDOWS_STATE_PATH`). (2) before-quit: `appIsQuitting = true` placed AFTER the `heldQuitForActiveWork` early-return gate and BEFORE upstream's `backendQuitTeardownDone` teardown (so no teardown-driven window close shrinks the saved set).
- **windows.ts:** brace re-knit — 3-way tangled two unclosed fns sharing one `}`; resolved keep-both (upstream `openSessionInTerminal` + our `closeAllSessionWindows`/`reopenSessionWindows`), each properly closed.
- **preload.ts / global.d.ts:** additive bridge/type keep-both (`closeAllSessionWindows`, `reopenSessionWindows`, `signalSessionGatewayReady`).
- **command-palette:** our session-window actions appended after upstream's items (comma added).
- **chat/index.tsx:** import merge (upstream's session-states/transcript-tail + our widened `@/store/windows` import).
- **i18n (6 locales):** keep-both (`reloadWindow` + our `closeAllSessionWindows`/`reopenSessionWindows`). No rename-key duplication (upstream's 4 rename keys are in distinct sections).

## Rename adoption (D-1)
- `session-actions-menu.tsx` left at pure-upstream, then added: `disableRename?: boolean` to `SessionActions`, into the `useSessionActions` destructure, and gated the rename `spec({...})` behind `...(disableRename ? [] : [...])`. `chat/index.tsx` already passes `disableRename={readOnly}` → flows through `SessionActionsMenu` → `useSessionActions`.
- `broadcastSessionsChanged` (Antigravity's retained delta): already upstream (`store/session-sync.ts`, wired into the rename flow) — inherited free, nothing added.
- Tail-page hydration (`577093def2`): inherited from the new base; orthogonal to our reopen queue (main-process window sequencing vs renderer transcript paging).

## Gates
- **3-tsconfig typecheck:** electron + e2e **CLEAN (exit 0)** — covers `main.ts`/`preload.ts`/`session-windows.ts`/`window-state.ts`. Base renderer clean for our files; only pre-existing `TS2307` missing-module errors (`driver.js`, `blobatar`) from stale node_modules.
- `git diff --check` clean; no conflict markers.
- **VITEST PENDING:** stale node_modules can't load `vite.config.ts` (missing a vite plugin the new base needs). Runs after `npm ci`, in the repack step.

## Questions
1. gateway.ts emitter placement + the secondary-gateway non-collision — confirm?
2. main.ts before-quit ordering (`appIsQuitting` before teardown) — correct?
3. Any concern with the `disableRename` gating approach on upstream's menu?
4. Anything you want re-verified once `npm ci` + vitest run in the repack?
