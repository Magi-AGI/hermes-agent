# Triad Review Request — Full Upstream Reconcile (2026-08-20)

**For:** Codex (authoritative on conflicts) + Antigravity
**Full plan to review:** `RECONCILE-2026-08-20-PLAN.md` (same folder)
**Nothing committed yet.** Bucket 1 applied to the working tree only, as a proof-of-method; all other buckets held for your verdict.

## Context (1 paragraph)
Advancing our fork overlay from base `590d547b4` (Aug 12) onto current upstream `b44871db0a` (`upstream/main`, +1856 commits) to absorb all Nous fixes and maximize stability. Method: fresh branch `magi/reconcile-2026-08-20` off upstream/main; re-apply our overlay bucket-by-bucket via 3-way apply of our BASE..HEAD diff (NOT a 32-commit linear rebase). Four parallel supersession analyses found: no bucket fully superseded; session-rename superseded (adopt upstream's, drop ours); code-0 fix + compaction guard still unique and needed.

## Bucket verdicts (detail in the plan)
1. **code-0 + RelayBuffer** — near-clean replay. PPID `os._exit(0)` bug still byte-identical/unfixed upstream; `host_supervisor.py` zero upstream drift; `relay_buffer.py` new; one `display_kind` kwarg auto-preserved by applying onto upstream. **APPLIED (working tree) as the worked example — please inspect.**
2. **STT** — near-clean; only dep delta is our `ctranslate2==4.7.2` pin; `uv.lock` regenerate-not-merge; `web_server.py` lifespan hook via anti-decorator-theft recipe.
3. **Bucket A (compaction guard)** — 1 HOT file (`auxiliary_client.py`). OAuth primitives all stable. Re-anchor the 702-line guard block + re-apply 12 per-rung hooks by function name + NEW `_assert_compression_route` on upstream's structured-output retry rung (`3c675019f1`) + verify custom `wrap_base` aliasing surfaces real host to the strict `base_url_host_matches`.
4. **Bucket B (desktop)** — 2 HOT files (`main.ts` +3710/-684, `gateway.ts` emitter into upstream's rewritten `reportPrimaryGatewayState`). Keep window-set/reopen-queue/voice-guard; drop rename (adopt upstream); tail-page `577093def2` inherited free. Watch upstream's unrelated secondary-*gateway* naming.

## Decisions already taken by the user (confirm or challenge)
- **D-1:** drop our rename plumbing, adopt upstream's; keep only `disableRename` gate + secondary-window titles.
- **D-2:** keep the guard's STRICT `api.anthropic.com` match — reject compaction via `/anthropic`-suffixed relay gateways that upstream now trusts (fail-closed cost safety).
- **D-3:** land near-clean buckets 1+2 (+ start A) this session; HOT desktop + deploy to follow-up.

## Questions
1. **Method:** fresh-branch bucket-by-bucket 3-way-apply vs linear rebase — right call?
2. **Keep/drop:** concur with D-1 (rename) and D-2 (strict host)?
3. **Bucket A:** any hole in the 702-block re-anchor + new-rung assert + custom-aliasing check? Is the new structured-output retry rung a real egress risk or belt-and-suspenders?
4. **Bucket B:** approve the hand-reapply of `main.ts`/`gateway.ts` (anchors survive, no redesign)?
5. **Gates:** is the per-bucket verification (targeted → full A/B; 3-tsconfig typecheck + vitest for B) adequate for the security-sensitive Bucket A?
