# Triad Review — Bucket A implementation (compaction route guard) — 2026-08-20

**For:** Codex (authoritative) + Antigravity. **Branch:** `magi/reconcile-2026-08-20` (B1+B2 committed; Bucket A applied to working tree, NOT yet committed — this review gates the commit).

## What was done
- All Bucket A files (`agent/auxiliary_client.py`, `anthropic_adapter.py`, `compaction_metrics.py`, `context_compressor.py`, `conversation_compression.py` + 7 test files) 3-way applied onto upstream with **ZERO conflicts** — anchors were byte-identical, as the supersession analysis predicted.
- Your review conditions folded in:
  1. **Dual retry-rung asserts (NEW code, not in our old diff):** upstream added a structured-output-rejection retry rung (`3c675019f1`) that our fork predates. I added `_assert_compression_route(resolved_provider, client)` before BOTH the sync (`_relay_sync_completion`) and async (`await _relay_async_completion`) retry egresses in `_call_llm_impl` / `_async_call_llm_impl`.
  2. **Aliasing rejection test (NEW):** `test_anthropic_suffixed_relay_gateway_rejected` — asserts `_is_anthropic_compatible_host("https://relay.example/anthropic")` is True (upstream trusts it) yet `_client_compression_route(_fake_anthropic_client(base_url="https://relay.example/anthropic"))` is None and `_compression_client_allowed(...)` is False (our strict guard rejects it). This is the case upstream's broadened helper would wave through; the pre-existing aliasing test used `openrouter.ai`, which upstream would reject anyway.
  3. **Confirmed present (not re-implemented):** `_get_cached_client` cache-hit screen; `except CompressionRoutingRejected` as the FIRST handler in `context_compressor._generate_summary` (line ~4762, ahead of the broad `except`, re-raises without recording a summary-failure / cooldown).

## Evidence (I own the test evidence — Codex avoided the runner)
- Guard suites: `test_compression_route_guard.py` + `test_compaction_route_part1.py` = **77 passed** (incl. the new aliasing test).
- Full `tests/agent/` **A/B = NET-ZERO**: baseline (clean upstream, guard stashed) **195 failed / 4605 passed**; ours **195 failed / 4683 passed**; `comm -23 ours baseline` = **empty (zero new failures)**. Ours +78 passing = the ported guard tests. The 195 are identical pre-existing/environmental failures (lsp, file_safety, command_token_source, transports — none in the guard path).
- `git diff --check` clean; all Bucket A files compile.

## Question
GO to commit Bucket A as-is? Any concern with the two new retry-rung asserts (placement/label) or the aliasing test's construction? Full evidence: `test-evidence/reconcile-2026-08-20-BA.txt`.
