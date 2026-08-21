# Full Reconcile onto Latest Upstream — Consolidated Plan (2026-08-20)

**Goal:** advance our overlay from base `590d547b4` (Aug 12) onto current upstream `b44871db0a` (`upstream/main`, +1856 commits), re-expressing our changes so we absorb ALL of Nous's fixes and maximize stability. Supersedes the stale `RECONCILIATION-PLAN.md`.

**Refs:** BASE `590d547b4` · UPSTREAM `b44871db0a` · OURS `1ffccb791d` (branch `magi/reconcile-2026-08-13`, 32 commits ≈ half docs).

**Method:** fresh branch `magi/reconcile-2026-08-20` off `upstream/main`; re-apply our overlay **bucket-by-bucket in risk order** (NOT a 32-commit linear rebase — that would cascade and re-introduce superseded work). Our existing triad-ratified commits are the source of truth for intent. Verification gate per bucket; one desktop repack + one deploy at the end.

---

## Headline verdicts (from 4 parallel supersession analyses)

| Bucket | Superseded? | Overall difficulty | Notes |
|---|---|---|---|
| **code-0 fix + RelayBuffer** | **NO** — PPID bug still byte-identical/unfixed upstream; no equivalent backpressure | **near-CLEAN replay** | `host_supervisor.py` 0 upstream drift; `relay_buffer.py` new; one `display_kind` kwarg to add in `compute_host.py` |
| **Bucket C (STT)** | **NO** — no upstream warm/hotwords/CUDA/ctranslate pin | **near-CLEAN + 1 regen** | `transcription_tools.py` 0-diff verbatim; `uv.lock` regenerate; `web_server.py` lifespan hook re-applies via recipe |
| **Bucket A (compaction guard)** | **NO** — upstream still has zero subscription-only enforcement; our guard is sole enforcement | **1 HOT file, rest MODERATE/CLEAN** | `auxiliary_client.py` HOT (702-block re-anchor + 1 new retry rung + custom-aliasing check); OAuth primitives all stable |
| **Bucket B (desktop)** | **PARTIAL** — session **rename/titles superseded** (adopt upstream, drop ours); window-set / reopen-queue / voice-guard NOT superseded | **2 HOT files, rest MODERATE** | `main.ts` (+3710/−684) & `gateway.ts` reapply-by-hand; tail-page is orthogonal & inherited free |

**Net:** two buckets are near-clean replays; two are real manual re-expression but every anchor survives (no redesign). We DROP one feature (rename) and INHERIT the tail-page OOM fix for free from the new base.

---

## KEEP / DROP / PORT table

### code-0 + RelayBuffer — KEEP ALL (near-clean replay)
- `tui_gateway/host_supervisor.py` — replay verbatim (upstream 0 drift). RelayBuffer hook + code-0 supervisor changes.
- `tui_gateway/relay_buffer.py` — new file, no upstream counterpart. Keep.
- `tui_gateway/compute_host.py` — replay our guard/exit-code/stdin-EOF work; **PORT:** add upstream's `display_kind=frame.get("display_kind") or None` kwarg at the prompt-submit call site (`3ead0f8dc1`).
- `tests/tui_gateway/test_compute_host_phase1.py` + `test_relay_buffer.py` — replay.

### Bucket C (STT) — KEEP ALL
- `tools/transcription_tools.py` (+test) — verbatim (0 upstream drift).
- `hermes_cli/config_defaults.py` — re-insert `hotwords`/`cuda_dll_dirs` keys after `initial_prompt`; leave upstream's `unload_after_idle_seconds` (our watcher consumes it).
- `tools/lazy_deps.py`, `pyproject.toml` — re-add `ctranslate2==4.7.2` pin (disjoint hunks).
- `uv.lock` — **REGENERATE** via `uv lock` (do not merge).
- `hermes_cli/web_server.py` — re-apply lifespan prewarm: helper `_warm_local_stt_model()` ABOVE the `@asynccontextmanager`, executor call AFTER `_warm_gateway_module()` (avoids the decorator-theft bug).
- `tests/hermes_cli/test_web_server.py` — take upstream (+462 new tests) as base, re-add our warm-tests.
- **Follow-up:** verify the pins-match test name (`test_packaging_metadata.py` vs our comment's `test_project_metadata.py`).

### Bucket A (compaction guard) — KEEP ALL
- `agent/compaction_metrics.py` (new), `agent/anthropic_adapter.py` (CLEAN) — replay.
- `agent/auxiliary_client.py` (**HOT**) — re-anchor the 702-line guard block as a unit (drops into clean space); re-apply the 12 per-rung `if _compression_guard_active()` hooks BY FUNCTION NAME (all byte-identical to base, shifted ~+600); **PORT:** add `_assert_compression_route` to the new structured-output-rejection retry rung (`3c675019f1`) in `_call_llm_impl`/`_async_call_llm_impl`; **VERIFY:** custom-provider `wrap_base` aliasing surfaces the real host to the strict `base_url_host_matches` check.
- `agent/context_compressor.py` (MODERATE) — re-anchor the `except CompressionRoutingRejected` handler BEFORE the broad `except`; verify ordering.
- `agent/conversation_compression.py` (MODERATE) — one hunk into `check_compression_model_feasibility`, no collision.
- tests — 2 new files verbatim; `test_auxiliary_client.py` mechanical re-anchor.
- **RECORD (intentional divergence):** upstream broadened `_is_anthropic_compatible_host` to trust `/anthropic`-suffixed gateways (MiniMax/Zhipu/LiteLLM). Our guard uses the STRICT `api.anthropic.com` match, so it will REJECT a compaction call routed to such a gateway even though upstream now trusts it. This is correct fail-closed posture (a relay gateway is not provably a subscription OAuth route) — **recommend keeping strict**; flagged for the record.

### Bucket B (desktop) — KEEP most, DROP rename plumbing
- **KEEP + reapply (absent upstream):** durable window set (`session-windows-state.json`), gateway-gated reopen queue (`signalSessionGatewayReady`/`waitForSessionWindowGatewayReady`/`REOPEN_WINDOW_GATEWAY_TIMEOUT_MS`), empty-capture voice guard (`use-voice-recorder.ts`, CLEAN drop-in).
  - `electron/window-state.ts` CLEAN; `session-windows.ts` near-clean; `store/windows.ts`/`preload.ts`/`global.d.ts` additive.
  - **HOT — hand-reapply:** `electron/main.ts` (11 hunks into reworked funcs, anchors survive), `store/gateway.ts` (emitter into upstream's rewritten `reportPrimaryGatewayState`; beware upstream's unrelated secondary-*gateway* naming).
  - `command-palette/index.tsx` re-anchor window commands; `use-desktop-integrations.ts`/`thread/list.tsx`/`chat/index.tsx` MODERATE.
- **DROP (superseded):** our session rename/title plumbing. Adopt upstream's rename UI (`RenameSessionDialog`, `renameSessionPreferringRpc`). **KEEP only:** the `disableRename` read-only gate (watch/spectator pop-outs) + secondary-window native `document.title`/headerless-header logic, re-applied onto upstream's menu.
- **i18n:** insert our 4 net-new keys (`noAudioCaptured`, `closeAllSessionWindows`, `reopenSessionWindows`, `resumeSucceeded`) into upstream's grown locale objects; DROP any of our rename/title keys now provided upstream. Run `i18n/runtime.test.ts`.
- **INHERITED FREE from new base:** tail-page hydration (`577093def2`) — the reopen OOM fix — is already in upstream; orthogonal to our reopen queue, composes cleanly.

---

## Sequencing (risk-ascending, verification gate after each)

1. **Setup:** branch `magi/reconcile-2026-08-20` off `upstream/main`. Copy in our design/tracking docs.
2. **Bucket 1 — code-0 + RelayBuffer** (near-clean): replay supervisor/relay/compute_host + `display_kind` port. GATE: `pytest tests/tui_gateway/` (expect the known pre-existing failure set, zero NEW).
3. **Bucket 2 — STT**: transcription verbatim, config/deps pins, `uv lock` regen, web_server hook, tests. GATE: `pytest tests/tools/test_transcription_tools.py tests/hermes_cli/test_web_server.py` + import smoke.
4. **Bucket 3 — Bucket A compaction guard** (HOT-Python): adapter/metrics clean → context_compressor/conversation_compression re-anchor → auxiliary_client 702-block + new-rung assert + aliasing check. GATE: new guard suites first, then targeted `-k "compress or compaction or auxiliary or anthropic or route_guard"`, then full `tests/agent/` A/B (net-zero vs known 140 pre-existing). **Triad review before commit** (security-sensitive).
5. **Bucket 4 — Bucket B desktop** (HOT-TS): additive/CLEAN files → gateway.ts surgical → main.ts per-function → command-palette → rename-adopt + disableRename → i18n merge. GATE: 3-tsconfig typecheck + `vitest --project {electron,ui}`. **Triad review** on gateway.ts + main.ts.
6. **Full verify:** whole `tests/` A/B; desktop repack (native `npm ci` node ≥22.22 + electron-builder).
7. **Deploy:** venv rebuild (`uv sync --extra all --locked` + STT LAZY_DEPS reinstall + CUDA validate) + desktop repack + relay restart. Contract check for the phone.

---

## Decisions needing user sign-off BEFORE laying commits

- **D-1 (Bucket B rename):** DROP our session-rename/title plumbing, adopt upstream's rename UI, keep only `disableRename` + secondary-title. [recommend: yes — upstream's is mature & maintained]
- **D-2 (Bucket A host strictness):** KEEP our guard's strict `api.anthropic.com` match, meaning compaction via a `/anthropic`-suffixed relay gateway is rejected even though upstream now trusts such gateways. [recommend: keep strict — fail-closed cost safety]
- **D-3 (scope this session):** land Buckets 1+2 (near-clean) + as much of 3 as review allows this session; HOT Bucket 4 (desktop) + final deploy likely spill to a follow-up. Checkpoint on the new branch. [recommend]
- **D-4 (old branch):** keep `magi/reconcile-2026-08-13` intact as fallback until the new branch is verified + deployed. [recommend: yes]
