# Cross-Session Compaction Queue — SPEC + Implementation Plan

- **Date:** 2026-07-11
- **Workstream:** Compaction Queue spec
- **Doc target:** `docs/plans/2026-07-11-compaction-queue-spec.md`
- **Base:** `lake/migrate-latest` @ `8bb1d5696e820f4ad2fdf623fbc7b443830e0394`
- **Branch (this doc):** `lake/compaction-queue-spec`
- **Mode:** Claude leads/writes. **Spec + implementation plan only — no implementation, no merge.**
- **Triad:** Claude lead/write · Codex r1, r2, r3 `changes_required` (all addressed) → **r4 `approve_with_notes`, no blockers** (note resolved in rev 5) · Gemini waived (`IneligibleTierError`). See §11.
- **Revision:** rev 6 — **Gate 0 FAILED against rev 5's substrate.** The coordinator moves from the profile-local `state.db` to a **root-anchored dedicated coordinator DB** (`<root>/compaction.db`), resolved via `get_default_hermes_root()` and mirroring the proven `kanban_home()` pattern (§3.1, §4.2, §8, Gate 0). Everything else in the design is **unchanged** — typed `SlotOutcome`/`SlotResult`, fail-open coordinator vs fail-closed routing, admission after the per-session lock and before `on_pre_compress`, hard-wall + wait-cap bypass, the status-emit move, and the non-routing invariant all stand. Rev 5 resolved Codex r4's mixed-source Anthropic note (scan-past, §5.3.1). Rev 4 corrected rev 3's mis-anchored Anthropic evidence and specified `CompressionRoutingRejected` propagation.

> ### ⚠ Rev 6 correction notice — rev 5's "state.db coordinates every backend" premise was WRONG
>
> Rev 5 asserted (§4.2, §8) that *"Only state.db already coordinates every backend process
> (desktop backend-pool, kanban workers, gateways share one WAL file)"*. **Gate 0 refuted
> this on source.** `state.db` is anchored to the **active profile's** `HERMES_HOME`
> (`hermes_state.py:123` → `get_hermes_home()`), and the two processes that generate the
> herd this feature exists to bound **deliberately span profiles**:
>
> - **Kanban workers:** a task's `assignee` **IS a profile name**, and the worker's
>   `HERMES_HOME` is derived from it — `env["HERMES_HOME"] = resolve_profile_env(profile_arg)`
>   where `profile_arg = normalize_profile_name(task.assignee)`
>   (`hermes_cli/kanban_db.py:7704`, `:7709–7720`). **The dispatcher herd is the
>   cross-profile case by construction.**
> - **Desktop backend-pool / gateway:** there is no `backend_pool` module — the desktop
>   backend is the `tui_gateway`, and in app-global remote mode **one backend serves every
>   profile** (`tui_gateway/server.py:1027`). It opens a **per-session** DB against a
>   **per-profile** path: `SessionDB(db_path=Path(profile_home) / "state.db")`
>   (`tui_gateway/server.py:1341–1353`), and spawns slash workers with
>   `env["HERMES_HOME"] = str(profile_home)` (`:278–304`). One process, many state.db files.
>
> **Consequence had we shipped rev 5:** `compaction_slots` would live in a *different file
> per profile*. `max_concurrent: 1` would have permitted **one concurrent compaction per
> profile**, silently — N profiles, N simultaneous compactions. Because the coordinator
> **fails open by design** (§4.3), a mis-scoped coordinator is **indistinguishable from a
> healthy idle one**: every acquire succeeds, every log line looks clean, and the feature
> bounds nothing. That is precisely the "queue that silently no-ops" this workstream is
> forbidden to build.
>
> **The fix already exists in-tree.** The kanban **board** hit this exact hazard and solved
> it by anchoring to the shared root rather than the active profile — `kanban_home()`
> (`hermes_cli/kanban_db.py:370–391`) resolves through `get_default_hermes_root()` precisely
> because *"Resolving the kanban paths through the active profile's `HERMES_HOME` would
> silently fork the board per profile, which breaks the dispatcher / worker handoff."*
> **`state.db` has no such anchoring; the coordinator now gets it.** Rev 6 is a **substrate
> relocation only** — the file the coordinator lives in changes; the mechanism (SQLite WAL,
> serialized writes, leased semaphore) and every other design decision do not.
>
> **Scope caveat carried forward (§4.8):** the *profile* was arguably never the right
> bounding unit either — the overload being prevented is **provider-side**, and a
> subscription account is typically shared across profiles. Root-scoping bounds
> **per-machine-root**, which is a strict improvement and matches the herd, but it is not
> per-account. See §4.8.

> ### ⚠ Rev 4 correction notice — rev 3's Anthropic hazard claim was WRONG
>
> Rev 3 claimed that on the compaction path the **metered `ANTHROPIC_API_KEY` is
> preferred first**, and escalated that as a live billing/privacy hazard. **That was
> incorrect.** It was anchored on `hermes_cli/auth.py::get_anthropic_key()`
> (`auth.py:486–503`), which **is not on the compaction path** — it serves CLI/setup
> surfaces only (`config.py:7909`, `doctor.py:1821`, `status.py:183`,
> `model_setup_flows.py:2851`).
>
> The **actual** compaction path is
> `auxiliary_client.py::_try_anthropic` (`:2610–2631`) →
> `anthropic_adapter.py::resolve_anthropic_token()` (`:1274–1321`), whose precedence
> **prefers OAuth/subscription sources first** and treats `ANTHROPIC_API_KEY` as the
> **last-resort** compatibility fallback (§3). So compaction does **not** silently
> prefer the metered key. Rev 3's alarm is **retracted**.
>
> **The real, narrower hazard stands** (Codex's framing, verified): `anthropic`
> remains a **dual-route** provider with an **API-key fallback at step 5**, and
> `resolve_anthropic_token()` returns a **bare string with no provenance** — so
> `_try_anthropic` cannot tell whether it received a subscription or a metered
> credential. With no OAuth source present and `ANTHROPIC_API_KEY` set, compaction
> **does** silently egress via the metered API. That is what the §5 guard must close.
>
> **Good news found while re-anchoring:** `anthropic_adapter._is_oauth_token()`
> (`:386–400`) **already** positively classifies OAuth vs Console API keys. The guard
> is therefore **smaller** than rev 3 specified — it reuses an existing primitive
> rather than inventing a prefix check.

---

## 1. Problem statement

Context compaction in Hermes is coordinated **per session only**. The existing guard
(`agent/conversation_compression.py::compress_context`, ~518–619) is a lease keyed on
`session_id`, held in the **profile-local `state.db`** (`compression_locks`,
`hermes_state.py:783`), whose sole job is to stop **two agents sharing one `session_id`**
(the parent-turn agent and its `background_review` fork) from racing a rotation. It is a
**per-session mutex**, not a concurrency governor across sessions, and it **fails open**.

> **Do not confuse this lock with the new queue coordinator.** The existing per-session
> `compression_locks` lease stays exactly where it is — in the profile-local `state.db` —
> because its scope genuinely *is* one session inside one profile, and both racers live in
> the same process/profile. The **new** cross-session semaphore (`compaction_slots`) is a
> different mechanism with a different scope and therefore a **different file**: the
> root-anchored `<root>/compaction.db` (§4.2). The two nest: per-session lock **outer**,
> compaction slot **inner** (§4.5).

There is **no coordination across sessions or backend processes**. When several large
sessions cross their compaction threshold at once — typically the kanban dispatcher
(`dispatch_in_gateway: true`, `dispatch_interval_seconds: 60`, `auto_decompose: true`,
`config.py:2708`) spawning a herd of backend workers — they **all fire compaction at the
same auxiliary provider simultaneously**. Each compaction is one large summarisation
call with a long timeout (floor 300s, `auxiliary_client.py:6005`; live config raised
`auxiliary.compression.timeout` to 600 + a Claude Max fallback). N at once overloads the
provider → timeouts → retries → more concurrent load: the compaction death-spiral the
600s timeout and fallback only partially mitigate.

The next layer is a **cross-session compaction queue**: bound concurrent compaction work
across all sessions and processes, **without** throttling normal agent behaviour.

## 2. Requirements & constraints

1. **No throttling of normal work.** Kanban dispatch and interactive turns run at full
   speed. **Only the compaction summarisation call is bounded.** Nothing on the normal
   turn path may block on the queue.
2. **In-place compaction only — never `/new`.** Support 200K+ sessions compacting in
   place (`compression.in_place: true`, `config.py:1463`; `archive_and_compact`,
   `hermes_state.py:3694`). A queued session must never drop context or start over.
3. **Subscription-only routes.** Compaction runs on the `openai-codex` OAuth route or the
   **Anthropic subscription/OAuth (Claude Max / Claude Code) route** only — never a
   metered route, including the **metered `anthropic` API-key route**, `openai-api`,
   OpenRouter, or Gemini. **Absolute for this workstream: no opt-out** (§5).
4. **Privacy.** Compaction inputs are business-sensitive. **No new external egress** — the
   coordinator is a **local SQLite file** (the root-scoped `<root>/compaction.db`, §4.2),
   never a network service. This constraint is what rules out per-account/cross-machine
   bounding (§4.8): that would require a network coordinator.
5. **Fail-open (coordinator).** If the coordinator is unavailable, degrade to today's
   per-session behaviour. **Never block or deadlock.** Deliberately asymmetric with
   constraint 3, which **fails closed** (§5.5).
6. **No new `HERMES_*` env vars for non-secret config** (`AGENTS.md:102–106`).

**Non-goal:** guaranteed ordering / interactive-over-background priority (§4.6).

## 3. Source evidence checked (against `8bb1d5696`)

### 3.1 Queue substrate
- **Per-session lock:** `conversation_compression.py` —
  `try_acquire_compression_lock` / `_CompressionLockLeaseRefresher` / `_release_lock`.
  TTL 300s, keyed on `session_id`, fails open. **Confirmed NOT cross-session.**
- **Lock primitives:** `hermes_state.py:2201–2333`. **Critical (drives §4.3):** these
  catch `sqlite3.Error` → return `False`/`None`, so **coordinator failure is
  indistinguishable from denial**. Safe for a per-session mutex; **unsafe to copy** for a
  global queue.
- **Cross-process substrate — CORRECTED in rev 6 (Gate 0).** state.db is a WAL SQLite file
  with serialized writes via `_execute_write`, but it is **profile-scoped, not
  machine-scoped**: `DEFAULT_DB_PATH = get_hermes_home() / "state.db"`
  (`hermes_state.py:123`), and `get_hermes_home()` returns `<root>/profiles/<name>` under a
  non-default profile (`hermes_constants.py:55–108`; `hermes_cli/profiles.py:2209–2225`,
  `:275`). It therefore coordinates **within one profile only**.
  - **Kanban workers span profiles:** `profile_arg = normalize_profile_name(task.assignee)`
    → `env["HERMES_HOME"] = resolve_profile_env(profile_arg)`
    (`hermes_cli/kanban_db.py:7704`, `:7709–7720`). The assignee **is** the profile, so the
    dispatcher herd — the motivating scenario (§1) — is inherently cross-profile.
  - **The desktop backend-pool is the `tui_gateway`, and one backend serves every profile**
    in app-global remote mode (`tui_gateway/server.py:1027`). It binds each session to that
    session's profile home and opens a per-profile DB:
    `SessionDB(db_path=Path(profile_home) / "state.db")` (`:1341–1353`); slash workers are
    spawned with `env["HERMES_HOME"] = str(profile_home)` (`:278–304`).
  - **⇒ A coordinator in `state.db` bounds per-profile and cannot see the herd.** The
    coordinator must live in a **root-anchored** DB (§4.2).
- **Import-time trap (drives §4.2 / §9.0):** `DEFAULT_DB_PATH` is evaluated at **module
  import** (`hermes_state.py:123`), and `set_hermes_home_override()` is a **ContextVar**
  that deliberately does not mutate `os.environ` (`hermes_constants.py:18–35`) — so it does
  **not** retroactively change `DEFAULT_DB_PATH`. That is exactly why the gateway must pass
  `db_path` explicitly (`tui_gateway/server.py:1353`). **The coordinator must resolve its
  path at call time, never rely on an import-time constant**, or it will silently bind to
  the *launch* profile while running inside a session bound to another.
- **Root-anchoring precedent (drives §4.2):** the kanban **board** already solved this.
  `kanban_home()` (`hermes_cli/kanban_db.py:370–391`) resolves via `get_default_hermes_root()`
  with an `HERMES_KANBAN_HOME` override, explicitly because *"Resolving the kanban paths
  through the active profile's `HERMES_HOME` would silently fork the board per profile,
  which breaks the dispatcher / worker handoff."* `get_default_hermes_root()`
  (`hermes_constants.py:113–133`) returns `<root>` when `HERMES_HOME` is
  `<root>/profiles/<name>`, and is import-safe (stdlib only).
- **Trigger sites:** `conversation_loop.py` → `_compress_context` at ~1034 (pre-API
  pressure), ~3131/~3386/~3609/~4786.
- **Pre-compaction side effect (drives §4.5):** `memory_manager.on_pre_compress` (~632)
  runs **before** `context_compressor.compress` (~639). `_emit_status(COMPACTION_STATUS)`
  fires at ~516, **before any lock/slot work** (drives §4.7).

### 3.2 Compression routing ladder (drives §5)
`_try_configured_fallback_chain` (`auxiliary_client.py:3901`, reads
`auxiliary.compression.fallback_chain`) → `_try_main_fallback_chain` (`:4023`, reads the
**top-level** `fallback_providers`) → `_try_main_agent_model_fallback` (`:3770`, the
user's **main agent** provider+model). `provider: auto` also enters built-in discovery.
**No subscription/auth filter on any rung.**

### 3.3 Anthropic — CORRECTED anchoring (rev 3 was wrong here)
- **The compaction call path is:** `auxiliary_client.py::_try_anthropic` (`:2610–2631`,
  reached from `:1853`, `:5236`, and `:4881` with `explicit_api_key`) →
  `agent.anthropic_adapter.resolve_anthropic_token()` + `build_anthropic_client()`
  (`:2612`, `:2631`).
- **`resolve_anthropic_token()` actual precedence** (`anthropic_adapter.py:1274–1321`,
  from its own docstring and body):
  1. `ANTHROPIC_TOKEN` (OAuth/setup token saved by Hermes)
  2. `CLAUDE_CODE_OAUTH_TOKEN`
  3. Claude Code credentials (`~/.claude/.credentials.json` + macOS Keychain, with
     automatic refresh — `read_claude_code_credentials()`, `:957`)
  4. Anthropic `credential_pool` OAuth entry (`~/.hermes/auth.json`)
  5. **`ANTHROPIC_API_KEY` — LAST**, described in-code as a "regular API key, **or a
     legacy OAuth token** saved in `ANTHROPIC_API_KEY` … compatibility fallback for
     pre-migration Hermes configs" (`:1315–1319`).
  **→ OAuth/subscription sources are preferred FIRST. Rev 3's "metered key wins first"
  claim is retracted.**
- **`hermes_cli/auth.py::get_anthropic_key()` (`:486–503`) is NOT on the compaction
  path.** Its metered-first precedence (`ANTHROPIC_API_KEY` → `ANTHROPIC_TOKEN` →
  `CLAUDE_CODE_OAUTH_TOKEN`) applies only to CLI/setup surfaces (`config.py:7909`,
  `doctor.py:1821`, `status.py:183`, `model_setup_flows.py:2851`). See §5.6 — a real but
  **separate, out-of-scope** inconsistency.
- **The residual hazard (verified, and what §5 must close):** `anthropic` is a **dual-route
  provider**. Step 5 is a genuine **API-key fallback**, and `resolve_anthropic_token()`
  returns a **bare string with no provenance** — `_try_anthropic` cannot tell which route
  it got. With no OAuth source and `ANTHROPIC_API_KEY` set, **compaction silently uses the
  metered API route.** Because step 5 may *also* legitimately hold a **legacy OAuth
  token**, the **source variable alone is not authoritative — the token's shape is.**
- **Existing classification primitive — reuse, don't invent:** `_is_oauth_token(key)`
  (`anthropic_adapter.py:386–400`) already positively identifies Anthropic OAuth tokens:
  `sk-ant-api*` → **False** ("Regular Anthropic Console API keys — x-api-key auth, never
  OAuth"); `sk-ant-` (non-`api`) / `eyJ` (OAuth JWT) / `cc-` (Claude Code OAuth access
  token) → **True**. Already used at `chat_completion_helpers.py:1559`,
  `account_usage.py:546`, `models.py:2733`. **The gap is not classification — it is that
  `resolve_anthropic_token()` discards provenance and `_try_anthropic` never checks.**

### 3.4 Summary-failure semantics (drives §5.5 / blocker on propagation)
- `context_compressor._generate_summary` (`:1747`) wraps the auxiliary call in a **broad
  `except Exception as e`** (`:2020`). Every branch ends in `return None` + a set
  `_last_summary_error` — which drives the **static-placeholder / middle-window-drop**
  path.
- **Second, worse escape:** at `:2142` the handler calls
  `_fallback_to_main_for_compression(e, "failed")` and **retries the summary on the MAIN
  MODEL** (`:2143`). A routing rejection raised inside the aux call would, under current
  code, be swallowed here and **re-attempted against the main agent model** — potentially
  a metered route. This is a routing escape hatch *inside* the compressor, not just a
  placeholder problem.
- `compression.abort_on_summary_failure` (`config.py:1428`) defaults to **`False`** —
  i.e. today's default **drops the middle window with a placeholder** on summary failure.
- **Existing carve-out precedent — reuse it:** `_last_summary_auth_failure` (`:1148–1154`)
  and `_last_summary_network_failure` (`:1155–1163`) are flags that make `compress()`
  **ABORT and preserve the session unchanged**, each documented as **"independent of the
  `abort_on_summary_failure` config flag"**. This is exactly the shape a routing refusal
  needs; §5.5 follows it rather than inventing a new mechanism.

### 3.5 Other
- `providers.py:62–66` — `openai-codex` **is** `auth_type="oauth_external"`
  (`base_url_override="https://chatgpt.com/backend-api/codex"`). Corroborating signal.
- `providers.py:101–104` / `auth.py:312–319` — `anthropic` is registry-typed
  `auth_type="api_key"`. **Confirms a provider-level `auth_type` guard is unusable for
  Anthropic** (it would reject Claude Max outright) — §5.2.
- **Discrepancy flagged:** the packet says `compression.in_place` defaults `False`; on this
  base it is `True` (`config.py:1463`). Design is independent of it.

## 4. Chosen design — the queue

*(Accepted by Codex at r2/r3; unchanged in rev 4 except §4.7 status handling.)*

### 4.1 Summary
A **leased semaphore** (`compaction_slots`) in a **root-scoped coordinator DB**
(`<root>/compaction.db`, §4.2 — *not* the profile-local `state.db`) bounds *concurrent
compaction summarisation calls* across all sessions, processes, **and profiles** under one
Hermes root, to a configurable limit (default **1**). It is **advisory admission control,
never a blocker**: it wraps inside the existing per-session lock, sits only on the
compaction path, and fails open on any coordinator error.

### 4.2 Why a ROOT-SCOPED coordinator DB (rev 6 — replaces "Why state.db")

**Substrate: `<root>/compaction.db`** — a dedicated SQLite WAL file anchored to the shared
Hermes **root**, not to the active profile's `HERMES_HOME`.

```python
def compaction_home() -> Path:
    """Root that anchors the compaction coordinator. Mirrors kanban_home()."""
    override = os.environ.get("HERMES_COMPACTION_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    from hermes_constants import get_default_hermes_root
    return get_default_hermes_root()

def compaction_db_path() -> Path:
    return compaction_home() / "compaction.db"
```

**Why root-anchored.** A queue that cannot see the herd is not a queue. The herd is
generated by kanban workers (whose `HERMES_HOME` is the **assignee's profile**) and by the
`tui_gateway` (which, in app-global remote mode, serves **every profile from one process**)
— see §3.1. A coordinator in profile-local `state.db` would live in a different file per
profile, so `max_concurrent: 1` would allow **one concurrent compaction per profile** while
logging perfectly clean acquires. Because the coordinator **fails open** (§4.3), that
failure is **invisible**. Root-anchoring is what makes the bound real.

**Why this exact pattern.** It is not novel — it is the fix the codebase already applied to
the kanban **board** for the identical hazard: `kanban_home()`
(`hermes_cli/kanban_db.py:370–391`) resolves through `get_default_hermes_root()` because
per-profile resolution *"would silently fork the board per profile, which breaks the
dispatcher / worker handoff."* We copy that shape verbatim, including the env override
(`HERMES_COMPACTION_HOME`, analogous to `HERMES_KANBAN_HOME`) for tests and Docker/custom
deployments. `get_default_hermes_root()` (`hermes_constants.py:113–133`) already normalises
`<root>/profiles/<name>` → `<root>` for both native and Docker layouts, and is import-safe.

**Why a DEDICATED db, not `kanban.db` and not `state.db`.**
- **Not `state.db`:** profile-scoped (the whole bug), and it is the hot session/message
  store — adding a semaphore's write traffic to the file that already suffers WAL
  write-lock contention (`hermes_state.py:871–900` documents visible TUI freezes from
  exactly this) is the wrong trade for a coordination table with no relational ties to
  sessions or messages.
- **Not `kanban.db`:** the board is root-scoped and would work, but compaction is **not a
  kanban concern** — CLI and desktop sessions with no kanban involvement must still be
  bounded. Piggybacking would couple an agent-wide privacy/perf control to an optional
  feature's schema and migration history.
- **A dedicated `compaction.db`** keeps the coordination table's lifecycle, schema version,
  and write traffic isolated. It is cheap: one small WAL file, created on first use.

**What is unchanged.** The *mechanism* is identical to rev 5 — SQLite WAL, serialized
writes, a leased semaphore with atomic acquire, TTL reclaim of crashed holders, and
idempotent release. **Only the file location changes.** No new process, no daemon, no
socket, **no new egress** (constraint 4). A gateway-owned in-memory queue still cannot see
sibling processes, which is why an in-process queue remains rejected (§8).

**Path resolution is call-time, never import-time.** Per the §3.1 import-time trap, the
coordinator resolves `compaction_db_path()` **when it is used**, never via a module-level
constant — otherwise a session bound to profile B (via the gateway's ContextVar override)
would coordinate against profile A's launch-time path. Root-anchoring makes this mostly
moot (the root is the same either way), but the discipline is required so a future
`HERMES_COMPACTION_HOME` override or a Docker layout cannot be captured at import.

### 4.3 Typed acquire outcomes
Copying `compression_locks`' `sqlite3.Error -> False` shape would make a broken coordinator
indistinguishable from a full queue → permanent no-compaction stall (the opposite of
fail-open). So:

```python
class SlotOutcome(enum.Enum):
    ACQUIRED          = "acquired"           # caller owns slot_id; must release
    DENIED            = "denied"             # queue genuinely full; caller defers
    COORDINATOR_ERROR = "coordinator_error"  # queue unusable; caller BYPASSES (fail-open)

@dataclass(frozen=True)
class SlotResult:
    outcome: SlotOutcome
    slot_id: Optional[str] = None
    error:   Optional[str] = None
```

- **`DENIED` only** when the transaction succeeded and observed `count >= max_slots`.
- **Any `sqlite3.Error`/unexpected exception → `COORDINATOR_ERROR`**, never collapsed into
  `DENIED`. Deliberately diverges from the neighbouring lock methods
  (`hermes_state.py:2281–2290`); requires a code comment saying so.
- The **call site** maps `AttributeError` (module/version skew — the historical
  no-progress-spin, `compress_context` ~571–585) and any other exception to
  `COORDINATOR_ERROR`.

| Outcome | Caller action |
|---|---|
| `ACQUIRED` | Start lease-refresher; compact; release in `finally`. |
| `DENIED` | **True no-op**: messages unchanged, set `first_pending_at`, no side effects, no status spam (§4.7). |
| `COORDINATOR_ERROR` | **Bypass the queue**, compact unbounded. Log once/session; alertable metric. |

Schema — lives in the **root-scoped `<root>/compaction.db`** (§4.2), **not** in the
profile-local `state.db`. `session_id` is recorded for diagnostics only; a `profile` column
is added so an operator can see *which* profiles are contending for the machine-wide bound
(diagnostics only — **never** a scheduling input, per §4.6):

```sql
CREATE TABLE IF NOT EXISTS compaction_slots (
    slot_id     TEXT PRIMARY KEY,   -- "0".."N-1"; row count is the bound
    holder      TEXT NOT NULL,      -- pid:tid:agent:nonce (same shape as lock holder)
    session_id  TEXT NOT NULL,      -- diagnostics only — NOT a scheduling input
    profile     TEXT NOT NULL DEFAULT '',  -- diagnostics only — which profile holds the slot
    source      TEXT NOT NULL DEFAULT '',  -- diagnostics only (platform / 'kanban')
    acquired_at REAL NOT NULL,
    expires_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_compaction_slots_expires ON compaction_slots(expires_at);
```

Acquire = one write-transaction: `DELETE` expired → `COUNT(*)` → `DENIED` if full, else
INSERT the lowest free `slot_id`. Companions: `refresh_compaction_slot` (bool is fine —
the shared `_CompressionLockLeaseRefresher` tolerates transient falsy refreshes within a
bounded ≤1-TTL give-up window), `release_compaction_slot` (idempotent),
`get_compaction_slot_load` (diagnostics).

### 4.4 Admission — enqueue early, never block, fail-open escapes
1. **Admit early.** The `compression.threshold` crossing (default 0.50; 85% autoraise on
   Codex 272K families) is the admission-attempt point. The headroom between "should
   compact" and "next request will not fit" **is the wait budget**.
2. **Try-acquire; never block the agent thread.** On `DENIED`, record `first_pending_at`
   and return messages unchanged for this cycle — today's lock-contended behaviour
   (~586–611). The turn/tool loop proceeds at full speed; the next natural re-check
   retries. **This is the no-throttle guarantee.**
3. **Hard-wall failsafe → bypass.** **Numeric, not call-site-defined**: when the pre-API
   request estimate meets/exceeds the point at which the next request will not fit once
   reserved output room is subtracted, compaction **bypasses the semaphore and runs
   immediately**, wherever evaluated.
   *Implementation requirement:* the PR must **name the exact token-budget
   variable/function**, and preserve the invariant that **threshold-triggered compaction
   stays queued while only true fit-failure paths bypass**. Reusing the threshold itself as
   the bypass trigger would make every compaction bypass the queue and silently void the
   feature; the two quantities must stay distinct.
4. **Wait cap → bypass.** Pending longer than `compaction_queue.max_wait_seconds` (default
   300s) → bypass **on the next natural re-check after that deadline elapses**
   (poll-on-re-check; *not* a "within 300s" latency guarantee).

### 4.5 Admission point inside `compress_context`
After the per-session lock; **before the first pre-compaction side effect**, which is
`memory_manager.on_pre_compress` (~632), *not* `context_compressor.compress` (~639).
Order: **per-session lock → compaction slot → `on_pre_compress` → `compress` → aux
client.** The slot is held across all of it **including fallback rungs** and released in
the existing outer `finally` (~980) beside `_release_lock()`, so fallback retries don't
double-count. `DENIED` returns with **zero side effects**.

### 4.6 Fairness — best-effort only
Priority is **withdrawn**; `priority`/`enqueued_at` are **not** in the schema. With
poll-retry admission, whoever polls first after a release wins — an interactive session
would not actually beat a background one, and a column no code reads would imply a
guarantee that does not exist. **Contract:** no ordering guarantee; opportunistic
admission; the only fairness property is **liveness** via the wait-cap bypass — starvation
is bounded by *fail-open*, not *scheduling*. Waiter-table scheduler deferred to §9.5,
gated on `wait_seconds{source}` metrics.

### 4.7 User-visible status on the queued path
`_emit_status(COMPACTION_STATUS)` currently fires at ~516, **before any lock/slot work**, so
a `DENIED` session would announce "Compacting context" and not compact, every re-check.

Required: **move the emit to after successful slot acquisition.** On `DENIED`, emit
**nothing on first denial**; only past `compaction_queue.notify_after_seconds` (default 60s)
emit a **deduplicated** queued status (e.g. "⏳ Compaction queued — waiting for a slot"),
reusing the once-per-session dedup pattern of `_last_compression_lock_warning_sid`. The
queued status must **not** carry `COMPACTION_STATUS_MARKER` — the gateway matches on it to
tag `kind="compacting"` (`tui_gateway/server.py::_status_update`), so including it would
show "Summarizing…" for a session that is merely waiting.

### 4.8 What the root bound does and does not cover (rev 6, honest scope)

Root-scoping bounds **per machine-root**: every profile under one `<root>` contends for the
same `max_concurrent` slots. That matches the herd (§3.1) and is what this feature needs.

It is **not** per-provider-account, and that gap is worth stating plainly rather than
discovering later. The overload being prevented is **provider-side** (constraint: a
compaction summarisation call is large and slow; N at once against one subscription account
is what spirals). A subscription account is typically **shared across profiles** — which
root-scoping now correctly bounds — but it may also be shared across **machines**, which no
local coordinator can see. Two laptops on one Claude Max account can still each run
`max_concurrent` compactions.

**Decision: accept this.** A cross-machine coordinator would need a network service — new
process, new failure surface, and **new egress**, which constraint 4 forbids outright. The
local root bound removes the realistic herd (one machine, many profiles, kanban dispatch
fan-out) and is a strict improvement over both today (unbounded) and rev 5 (per-profile).
Per-account bounding is **explicitly out of scope**; if `wait_seconds` metrics ever show
cross-machine contention is real, that is a separate, separately-argued change.

## 5. Subscription-only routing — invariant + required guard

### 5.1 What the queue guarantees (non-routing invariant)
The queue layer is **strictly non-routing**: it never calls `_resolve_task_provider_model`,
never builds a client, never passes `provider`/`model`/`base_url`/`api_key`, never inspects
or mutates a fallback chain. Its only effects are *when* `compress_context` proceeds and
*whether* it proceeds this cycle. Provider selection is byte-for-byte identical with the
queue enabled, disabled, denied, or failed-open. Test-enforced (§9.6).

### 5.2 The gap — verified (with rev 3's overclaim removed)
Preserving current routing does **not** satisfy constraint 3:

- The ladder (§3.2) reaches OpenRouter / Gemini / any credentialed `api_key` provider / the
  main agent model, with **no** subscription filter on any rung.
- **Anthropic is a dual-route provider.** `resolve_anthropic_token()` prefers OAuth first
  (so rev 3's "metered-first" alarm was **wrong**), **but step 5 is a real API-key
  fallback**, and the function returns **no provenance**. With no OAuth source and
  `ANTHROPIC_API_KEY` set, compaction silently egresses via the **metered** API — and no
  caller can tell. Additionally, step 5 may legitimately hold a **legacy OAuth token**, so
  **the source variable is not authoritative; the token shape is** (`_is_oauth_token`).
- **A provider-level `auth_type` guard is unusable:** `anthropic` is registry-typed
  `auth_type="api_key"` (§3.5), so such a check would **reject Claude Max outright** or, if
  loosened by name, **admit the metered route**.

**⇒ Enforcement must be on the (provider, auth-mode-actually-resolved) ROUTE.**

### 5.3 The guard — an explicit ROUTE allowlist

```
ALLOWED COMPACTION ROUTES (default):
  (openai-codex, oauth_subscription)   # ChatGPT Pro OAuth (registry auth_type=oauth_external)
  (anthropic,    oauth_subscription)   # Claude Max / Claude Code OAuth ONLY

EXPLICITLY REJECTED (non-exhaustive):
  (anthropic,    api_key)              # metered ANTHROPIC_API_KEY (resolve step 5)
  (openai-api, *) (openrouter, *) (gemini, *) (any api_key provider, *)
  (<main agent provider>, *)           # unless it independently matches an allowed route
```

**Where the provenance resolver lives (Codex r3 blocker 2):**

1. **`agent/anthropic_adapter.py`** — add, beside `resolve_anthropic_token()` (`:1274`), a
   provenance-returning sibling, e.g.:
   ```python
   def resolve_anthropic_token_with_provenance() -> Optional[AnthropicCredential]:
       """Same resolution ORDER as resolve_anthropic_token(), but reports where the
       token came from and whether it is subscription-backed."""
       # -> (token, source, mode)
       #    source ∈ {ANTHROPIC_TOKEN, CLAUDE_CODE_OAUTH_TOKEN, claude_code_credentials,
       #              credential_pool, ANTHROPIC_API_KEY}
       #    mode   = "oauth_subscription" if _is_oauth_token(token) else "api_key"
   ```
   - **Reuse `_is_oauth_token()` (`:386–400`) as the authoritative classifier** — it already
     distinguishes `sk-ant-api*` (Console API key, never OAuth) from `sk-ant-`/`eyJ`/`cc-`
     OAuth tokens. **Token shape decides the mode; the source var is reported for
     diagnostics/logging only.** This correctly handles both traps: an `ANTHROPIC_TOKEN` that
     actually holds an API key (→ `api_key` → rejected) and a **legacy OAuth token parked in
     `ANTHROPIC_API_KEY`** (→ `oauth_subscription` → allowed), which a source-var-only rule
     would get backwards in both directions.
   - **Keep `resolve_anthropic_token()`'s order and behaviour unchanged** — many callers
     depend on it (`agent_init.py:763`, `agent_runtime_helpers.py:1901`, `run_agent.py:4390`,
     `runtime_provider.py:1379/1903`, `models.py:2737`, `account_usage.py:546`,
     `chat_completion_helpers.py:1559`). The new function is additive; ideally
     `resolve_anthropic_token()` becomes a thin wrapper returning just `.token`.

   **Mixed sources — SCAN PAST metered candidates, don't fail fast (Codex r4 note).**
   The compaction resolver **walks the same precedence order (1→5) but does not stop at the
   first token found.** It evaluates each candidate's mode, **skips** any that is metered-shaped
   (`_is_oauth_token(token) is False`), and **continues to the next source**, returning the
   **first `oauth_subscription` candidate**. It fails closed (§5.5) **only when every source is
   exhausted** with no allowed subscription credential.

   This is **required, not merely nicer**, and the reason is structural:
   `resolve_anthropic_token()` **short-circuits** — each step is `if token: return token`
   (`anthropic_adapter.py:1290–1319`). So a metered-shaped value sitting in `ANTHROPIC_TOKEN`
   (step 1) means **steps 3–4 are never reached**. A fail-fast policy would therefore let one
   stray env var **mask a perfectly good Claude Max credential** in the Claude Code credential
   store or the credential pool, and needlessly fail-close compaction for a user who *does*
   have a valid subscription route. Scan-past keeps the privacy guarantee **identical** (a
   metered token is never used, only skipped) while letting a later valid OAuth source save the
   compaction.

   Note the credential pool (step 4) carries a **structural** provenance signal independent of
   token shape: `_resolve_anthropic_pool_token()` (`:1259–1261`) already filters entries on
   `auth_type == AUTH_TYPE_OAUTH`. Prefer that structural signal where present; fall back to
   `_is_oauth_token()` shape classification for the env-var and credential-file sources.

   Every skipped candidate is logged and counted (§7) — a user whose `ANTHROPIC_TOKEN` is
   metered-shaped should be able to see that it was skipped, not silently ignored.
2. **`agent/auxiliary_client.py::_try_anthropic` (`:2610–2631`) is the runtime compaction
   call path that consumes it.** For the **compression task**, `_try_anthropic` must resolve
   *with provenance*, and:
   - **reject** a `mode == "api_key"` credential — do **not** build a client, do **not**
     issue a request; skip to the next allowed route, else raise
     `CompressionRoutingRejected` (§5.5);
   - **never fall through to `ANTHROPIC_API_KEY`** as a metered credential for compaction
     (step 5 remains available only when the token there is *provably* an OAuth token by
     `_is_oauth_token`);
   - note `_try_anthropic(explicit_api_key=...)` (called from `:4881`) must apply the **same**
     check to the explicit key — an explicit metered key is still a metered route.
3. **No `hermes_cli/auth.py` change is required for this guard.** `get_anthropic_key()` is
   not on the compaction path (§3.3). Rev 3 wrongly put the fix there. See §5.6 for the
   separate issue that *does* live in `auth.py`.
4. **Verify the constructed client, not just the credential.** Assert the client built for
   the call carries the expected subscription route (base URL + OAuth auth header — note
   `build_anthropic_client()` (`:703`) already auto-detects setup-tokens vs API keys), so an
   allowlisted provider name aliased to a metered endpoint via `base_url`/`api_key` override
   (permitted by `_resolve_task_provider_model`, `auxiliary_client.py:5969–5987`) is caught.
   Registry `auth_type` is a **corroborating signal for `openai-codex` only** — never
   sufficient alone, and **useless for `anthropic`**.

**Enforcement point:** every candidate on **every rung** — primary resolution,
`_try_configured_fallback_chain`, `_try_main_fallback_chain`,
`_try_main_agent_model_fallback`, and `auto` discovery — slotted beside the existing
`_is_provider_unhealthy` / context-window screens that already skip candidates in those same
loops. Non-conforming candidates are **skipped** (logged, counted), never used.

**Startup validation:** at `check_compression_model_feasibility`, validate the configured
provider and every configured fallback rung against the route allowlist; warn/reject early
rather than at first compaction.

### 5.4 No opt-out
The route allowlist has **no disable value**. An empty/malformed `allowed_routes` is a
**configuration error** — it fails startup validation and compaction fails closed (§5.5);
it does **not** disable the guard. Users may **narrow** the list (e.g. Codex only) but never
**widen** it to a metered route; a metered entry is rejected at config load with an error
naming it. Any "metered compaction" mode is **out of scope** — that would be a separate,
explicitly-argued change to this privacy posture, not a flag smuggled in here.

### 5.5 Fail-CLOSED on routing — distinct signal + propagation (Codex r3 blocker 1)

> **Coordinator unavailable → fail OPEN (compact unbounded).
> No allowed subscription route → fail CLOSED (do not compact; never egress to a metered
> route).**

**The propagation problem (verified, §3.4).** `_generate_summary` wraps the auxiliary call
in a **broad `except Exception`** (`context_compressor.py:2020`). A `CompressionRoutingRejected`
raised inside the aux/provider call would be swallowed there and converted into **either**:
- the generic summary-failure path → `_last_summary_error` → **static placeholder +
  middle-window drop** (default, since `abort_on_summary_failure` is `False`); **or, worse**,
- `_fallback_to_main_for_compression(e, "failed")` at `:2142` → **retry the summary on the
  MAIN MODEL** (`:2143`) — a **second routing escape** that could send business-sensitive
  compaction content to a metered main-agent route, i.e. the exact egress we refused.

**Requirements:**

1. **`agent/context_compressor.py` is a file-to-touch** (rev 3 omitted it).
2. **`CompressionRoutingRejected` must bypass `_generate_summary`'s broad handler** — either
   re-raised as the **first** clause (`except CompressionRoutingRejected: raise`) *before*
   the `except Exception as e` at `:2020`, or explicitly checked at the top of that handler
   and re-raised. It must **never** reach the generic classification logic.
3. **It must never trigger the main-model fallback** (`_fallback_to_main_for_compression`,
   `:2142–2143`). A routing refusal is not a provider fault to route around.
4. **It must not be recorded as a summary failure:** not set `_last_summary_error`, not arm
   the generic summary-failure cooldown.
5. **`compress()` must ABORT and preserve the session unchanged**, following the **existing
   carve-out precedent** — `_last_summary_auth_failure` (`:1148–1154`) and
   `_last_summary_network_failure` (`:1155–1163`), both documented as **"independent of the
   `abort_on_summary_failure` config flag"**. Add `_last_summary_routing_rejected` in the
   same shape, so `compress()` sets `_last_compress_aborted = True` and returns messages
   unchanged. **This reuses a proven mechanism rather than inventing one.**
6. **Consequently, in `compress_context`:** messages unchanged; **no static placeholder; no
   middle-window drop; no `archive_and_compact`; no session rotation** — **regardless of
   `compression.abort_on_summary_failure`**. That flag is a *summary-quality* policy and has
   **no authority** over a *privacy* refusal.
7. **Distinct user-facing message** naming cause (no allowed subscription route for
   compaction) and remedy (authenticate Codex / Claude Max) — not the generic "compression
   summary failed" text — plus its own metric (§7).

**Net effect:** the session **freezes at its current size with full context intact** rather
than egressing to a metered provider *or* silently losing its middle window. Neither
outcome loses context; neither forces `/new`.

### 5.6 Out of scope, but worth filing separately
`hermes_cli/auth.py::get_anthropic_key()` (`:486–503`) prefers **`ANTHROPIC_API_KEY` first**,
while the runtime resolver `resolve_anthropic_token()` prefers **OAuth first**. These two
disagree. `get_anthropic_key()` feeds `doctor`, `status`, `config`, and the setup flows — so
those surfaces can **report a different credential than the runtime actually uses** (e.g.
doctor says "using ANTHROPIC_API_KEY" while compaction really runs on Claude Max OAuth, or
vice-versa). That is a **diagnostics-fidelity bug, not a compaction egress bug**, and it is
**not** what rev 3 claimed it was. Recommend a separate issue; **explicitly out of scope
here.**

## 6. Config surface

All tunables are YAML; **no new `HERMES_*` env var for non-secret config** (AGENTS §102–106).

> **One `HERMES_*` addition, and why it does not violate that rule (rev 6).**
> `HERMES_COMPACTION_HOME` (§4.2) is **not config** — it is a *path override* for tests and
> unusual deployments (Docker, custom roots), in the same class as the **already-existing**
> `HERMES_KANBAN_HOME` (`hermes_cli/kanban_db.py:386`) and `HERMES_HOME` itself. It tunes no
> behaviour; it only relocates the coordinator file. It is required because the multi-process
> Gate 0 / concurrency tests must point real subprocesses at a temp root **without** touching
> the developer's real `<root>/compaction.db`, and `HERMES_HOME` cannot serve that purpose —
> root-anchoring deliberately *ignores* per-profile `HERMES_HOME`, which is the entire point.
> No queue *tunable* (`max_concurrent`, TTLs, waits) gets an env var; those stay YAML-only.

```yaml
compaction_queue:
  enabled: false              # master switch. DEFAULT FALSE through implementation — the
                              # queue ships dark and is enabled only as a separate,
                              # explicitly user-approved rollout step (§9.4). false ->
                              # today's per-session-only behaviour, i.e. no change.
  max_concurrent: 1           # cap on simultaneous compaction summarisation calls across
                              # ALL sessions, processes, AND PROFILES under one Hermes root
                              # (§4.2). Clamped >= 1 (0 would deadlock all compaction,
                              # violating fail-open).
  max_wait_seconds: 300       # a pending session bypasses on its next re-check AFTER this
                              # elapses. Best-effort liveness, not a latency guarantee.
  slot_ttl_seconds: 300       # lease TTL; crashed holders reclaimed after this.
  notify_after_seconds: 60    # only surface a "compaction queued" status if still pending
                              # this long. First denial is silent (§4.7).
  # NOTE: the coordinator's location is NOT configurable in YAML. It is always
  # <root>/compaction.db, resolved at call time via get_default_hermes_root()
  # (override: HERMES_COMPACTION_HOME, above). Making it a YAML key would let one
  # profile's config.yaml silently point that profile at a different coordinator file —
  # reintroducing the exact per-profile fork Gate 0 rejected.

auxiliary:
  compression:
    # Subscription-only ROUTE guard (§5). PREREQUISITE for enabling the queue.
    # A route is (provider, auth-mode-ACTUALLY-RESOLVED) -- NOT a provider name, and NOT
    # the registry's provider-level auth_type (which types `anthropic` as api_key, §5.2).
    # For anthropic the mode is decided by anthropic_adapter._is_oauth_token() on the
    # resolved token -- token SHAPE is authoritative, not which env var supplied it.
    # Enforced on every rung: primary, fallback_chain, top-level fallback_providers,
    # main-agent fallback, and `auto` discovery.
    #
    # THERE IS NO DISABLE VALUE (§5.4). Empty/malformed = config error, not an opt-out.
    # Narrowing allowed; widening to a metered route is rejected at config load.
    allowed_routes:
      - provider: openai-codex
        auth_mode: oauth_subscription      # ChatGPT Pro OAuth
      - provider: anthropic
        auth_mode: oauth_subscription      # Claude Max / Claude Code OAuth ONLY. The
                                           # metered ANTHROPIC_API_KEY route is rejected
                                           # even though the provider name matches.
```

`compaction_queue.*` changes **timing only, never routing** (§5.1). `enabled: false` kills
the queue but **not** the route guard — the guard is a privacy control, independent of the
performance feature.

## 7. Observability

- `compaction_queue.slots_in_use` / `.max` (gauge).
- `compaction_queue.wait_seconds` (histogram, **labelled by `source`**) — `now -
  first_pending_at` at acquisition. The `source` split is what would later justify or refute
  a priority scheduler (§4.6 / §9.5).
- `compaction_queue.denied_total` — `DENIED` only.
- `compaction_queue.failopen_total{reason=hardwall|waitcap|coordinator_error}` —
  **`coordinator_error` must be alertable**: it means the queue is silently not queueing.
- `compaction_queue.reclaimed_expired_total`.
- **`compaction.route_rejected_total{provider, reason=not_allowlisted|metered_auth_mode|endpoint_mismatch}`
  — must be alertable.** `{provider=anthropic, reason=metered_auth_mode}` firing means the
  §5.2 dual-route fallback was hit in production.
- **`compaction.credential_candidate_skipped_total{provider, source, reason=metered_shape}`** —
  a candidate credential was **skipped** during the scan-past walk (§5.3.1), e.g. a
  metered-shaped `ANTHROPIC_TOKEN` passed over in favour of a later Claude Code OAuth
  credential. Distinct from a *rejection* (which ends the route): a skip means the search
  continued. Non-zero with a **successful** compaction is benign-but-worth-surfacing — it tells
  a user their `ANTHROPIC_TOKEN` is metered-shaped and inert for compaction. Non-zero
  **alongside** `routing_rejected_abort_total` is the diagnostic pair meaning "we skipped
  metered candidates and then found no subscription route at all".
- **`compaction.routing_rejected_abort_total`** — fail-closed aborts (§5.5), distinct from
  summary failures.
- INFO log on acquire/release (`slot_id`, `session_id`, `profile`, `source`, `waited_ms`) and
  on route selection — **which provider, which credential SOURCE, and which auth-mode** (the
  provenance that does not exist today). WARN once/session on `COORDINATOR_ERROR`.
- **INFO log the RESOLVED coordinator path once per process at first acquire (rev 6).** The
  rev-5 defect class — a coordinator silently bound to the wrong (per-profile) file — is
  invisible under fail-open semantics. Printing the actual `<root>/compaction.db` in use is
  the cheapest way for an operator to confirm the bound is real, and for two herd processes
  to be compared. `profile` is on the slot row (§4.3) for the same reason: so an operator can
  see cross-profile contention actually being arbitrated.
- Slot load in diagnostics (`hermes_cli/kanban_diagnostics.py`) — note the coordinator is
  root-scoped, so the diagnostic reports the **machine-root-wide** load, not this profile's.

## 8. Rejected alternatives

- **Gateway-owned in-process queue** — cannot see sibling backend processes. Worse than it
  first appears: in app-global remote mode **one gateway serves every profile**
  (`tui_gateway/server.py:1027`) yet kanban workers are **separate processes**
  (`hermes_cli/kanban_db.py:7709–7720`), so an in-process queue would miss the entire
  dispatcher herd — the exact thing being bounded.
- **The profile-local `state.db` as the coordinator (rev 5) — REJECTED BY GATE 0.** Its
  stated rationale, *"state.db already coordinates cross-process for free"*, is **false**:
  state.db is profile-scoped (`hermes_state.py:123` → `get_hermes_home()`), while the herd
  is cross-profile by construction (kanban `assignee` **is** a profile; the desktop backend
  serves every profile). It would have bounded **one compaction per profile** while
  fail-open semantics made the mis-scoping **invisible**. Replaced by the root-anchored
  `<root>/compaction.db` (§4.2). *This is the rev-6 correction; see the notice at the top.*
- **Piggybacking the root-scoped `kanban.db`** — it is correctly root-anchored and would
  technically work, but compaction is not a kanban concern (CLI/desktop sessions with no
  kanban involvement must still be bounded), and it would couple an agent-wide control to an
  optional feature's schema/migrations. A dedicated `compaction.db` is cleaner for the same
  cost (§4.2).
- **Dedicated coordinator service/socket** — new process, new failure surface, and potential
  **new egress** (constraint 4). A root-anchored SQLite WAL file coordinates every local
  process for free, with no daemon.
- **Cross-machine / per-provider-account bounding** — would require a network service, i.e.
  new egress; explicitly out of scope (§4.8).
- **Central FIFO/priority queue with waiter table + scheduler** — deferred, not dismissed
  (§4.6, §9.5).
- **Copying `compression_locks`' `sqlite3.Error -> False` acquire shape** — converts
  fail-open into a permanent no-compaction stall (§4.3).
- **Provider-name-only allowlist, or provider-level `auth_type` guard (rev 2)** — rejected on
  source: `anthropic` is registry-typed `api_key`, so it would reject Claude Max or admit the
  metered route (§5.2).
- **Source-variable-only auth classification (rev 3)** — rejected on source: `ANTHROPIC_API_KEY`
  may hold a **legacy OAuth token** and `ANTHROPIC_TOKEN` may hold an API key, so the env var
  is not authoritative. **Token shape via `_is_oauth_token()` is** (§5.3.1).
- **Patching `hermes_cli/auth.py::get_anthropic_key()` to fix compaction routing (rev 3)** —
  rejected: it is **not on the compaction path** (§3.3). Its own inconsistency is a separate
  diagnostics bug (§5.6).
- **Any guard opt-out / metered-compaction mode** — contradicts the hard privacy constraint
  (§5.4).
- **Routing rejection reported as a generic summary failure** — would hit the static-placeholder
  path *and* the main-model retry at `context_compressor.py:2142` (§5.5).
- **Throttling the kanban dispatcher** — violates constraint 1 and misses interactive sessions.
- **Blocking acquire on the agent thread** — stalls the turn/tool loop; violates constraint 1.

## 9. Implementation plan

Target branch: `lake/migrate-latest`. **This doc implements nothing.**

### 9.0-GATE — Gate 0: prove the coordinator path is shared (**BLOCKING**, rewritten rev 6)

> **Gate 0 as written in rev 5 — "prove every backend shares the same `state.db`" — was run
> and FAILED.** It is replaced by the check below. **No queue code may be written until this
> gate passes.** The gate exists because the failure mode is *silent*: a mis-scoped
> coordinator fails open, so every acquire succeeds and every log looks healthy while the
> bound does nothing.

**Claim to prove:** every process in the herd — a **desktop-pool / `tui_gateway` backend**
(including one serving a *non-launch* profile), a **kanban worker** (whose `HERMES_HOME` is
its task's **assignee profile**), and a **CLI/gateway agent** — resolves the **same
root-scoped coordinator path** `<root>/compaction.db`, **regardless of each process's
per-profile `HERMES_HOME`**.

**How to prove it (must be a real, executable check, not a source-reading exercise):**
1. Create a temp root with **≥2 distinct profiles** (`<root>/profiles/a`, `<root>/profiles/b`).
2. Launch **real subprocesses** — not threads, not mocks — with the *divergent* `HERMES_HOME`
   values the herd actually uses: one at `<root>/profiles/a`, one at `<root>/profiles/b`, one
   at `<root>` itself.
3. Have each print its **resolved** `compaction_db_path()` (and, for contrast, its resolved
   `state.db` path).
4. **Assert positively that the coordinator paths are EQUAL** across all three, and that they
   equal `<root>/compaction.db`. Assert the `state.db` paths **differ** — that contrast is the
   regression test for the rev-5 bug, and it is what makes the gate meaningful rather than
   tautological.
5. **Do not mock** `get_hermes_home`, `get_default_hermes_root`, or `SessionDB`. The bug being
   guarded against is *path resolution*, so mocking path resolution would assert nothing.

**Fail condition:** if any herd process resolves a different coordinator path, **STOP** — the
substrate is wrong again and the design needs revision, not a workaround.

**Carry-forward hazard (from the Gate 0 investigation, §3.1):** `hermes_state.DEFAULT_DB_PATH`
is frozen at **import time** and `set_hermes_home_override()` is a ContextVar that does not
update it. The coordinator must resolve its path **at call time**. A test should assert that
importing the coordinator module under one `HERMES_HOME` and then calling it under a
ContextVar override still yields the root path.

### 9.0 Phase 0 — Slot primitives (no behaviour change; substrate corrected in rev 6)
**New module `agent/compaction_coordinator.py`** (NOT `hermes_state.py` — that class is bound
to the profile-local DB, which is the bug Gate 0 caught):
- `compaction_home()` / `compaction_db_path()` — root anchoring via `get_default_hermes_root()`
  with the `HERMES_COMPACTION_HOME` override, mirroring `kanban_home()`
  (`hermes_cli/kanban_db.py:370–391`). **Resolved at call time, never an import-time constant.**
- `compaction_slots` DDL + index (§4.3) against `<root>/compaction.db`; WAL + serialized
  writes; schema versioned independently of `state.db`'s `SCHEMA_VERSION`.
- `SlotOutcome` / `SlotResult`; `try_acquire_compaction_slot`, `refresh_compaction_slot`,
  `release_compaction_slot`, `get_compaction_slot_load`.
- Code comment at the `except sqlite3.Error` site explaining why it returns
  `COORDINATOR_ERROR` and must **not** be harmonised with `hermes_state.py`'s neighbouring
  lock methods' `-> False` shape (§4.3).
- Pure additions, **no callers** — and `compaction_queue.enabled` stays **false** (§6), so
  Phase 0 cannot change behaviour even accidentally. The InstallDir runs the live backend;
  shipping dark is what keeps that safe.

### 9.1 Phase 0.5 — Subscription-only ROUTE guard (**BLOCKING PREREQUISITE**, §5)
Standalone privacy fix; worth landing on its own merits even if the queue never ships.

**Files to touch (corrected in rev 4):**
- **`agent/anthropic_adapter.py`** — add `resolve_anthropic_token_with_provenance()` beside
  `resolve_anthropic_token()` (`:1274`), reusing `_is_oauth_token()` (`:386`) as the
  authoritative mode classifier. Leave `resolve_anthropic_token()`'s order/behaviour
  unchanged for its many existing callers (§5.3.1).
- **`agent/auxiliary_client.py`** — `_try_anthropic` (`:2610–2631`, incl. the
  `explicit_api_key` path from `:4881`) consumes the provenance resolver and **rejects
  `mode == "api_key"` for compaction**. Apply the route check to **every rung**
  (`_try_configured_fallback_chain`, `_try_main_fallback_chain`,
  `_try_main_agent_model_fallback`, `auto` discovery) beside the existing
  `_is_provider_unhealthy` / context-window screens. Add the constructed-client
  endpoint/auth verification (§5.3.4). Raise `CompressionRoutingRejected` on exhaustion.
- **`agent/context_compressor.py`** — **(rev 4 addition, Codex r3 blocker 1)** re-raise
  `CompressionRoutingRejected` **before** `_generate_summary`'s broad `except Exception`
  (`:2020`); ensure it **never** reaches `_fallback_to_main_for_compression` (`:2142`), is
  **not** recorded as `_last_summary_error`, and sets `_last_summary_routing_rejected`
  following the existing `_last_summary_auth_failure` / `_last_summary_network_failure`
  carve-out shape (`:1148–1163`) so `compress()` aborts unchanged.
- **`agent/conversation_compression.py`** — `compress_context` honours the abort:
  messages unchanged, **no placeholder, no middle-window drop, no `archive_and_compact`, no
  rotation, independent of `abort_on_summary_failure`**; distinct user-facing message.
- **`hermes_cli/config.py`** — `auxiliary.compression.allowed_routes` (default: the two routes
  in §6). Load-time validation: reject metered entries; reject empty/malformed (**no opt-out**).
- **`check_compression_model_feasibility`** — startup route validation.
- **NOT `hermes_cli/auth.py`** — not on the compaction path (§3.3, §5.6).

**The queue must not be enabled until this phase is merged and its tests pass.**

### 9.2 Phase 1 — Queue config
`compaction_queue` block in `DEFAULT_CONFIG` with **`enabled: false`** (§6); add to the
top-level section allowlist (`config.py:5211`); clamp `max_concurrent >= 1`. Populate agent
fields at init (beside `_compression_lock_ttl_seconds`). The coordinator **path is not a YAML
key** (§6) — it is always `<root>/compaction.db`, so no profile's `config.yaml` can point that
profile at a different coordinator file and silently re-fork the bound.

### 9.3 Phase 2 — Wire into compaction (behind `enabled`)
- `compress_context`: acquire after the per-session lock, **before `on_pre_compress`**
  (§4.5); start refresher; release in the outer `finally`; handle the three outcomes (§4.3),
  incl. call-site `AttributeError` → `COORDINATOR_ERROR`. `DENIED` reuses the lock-contended
  return-unchanged path and sets `first_pending_at`.
- **Move the `COMPACTION_STATUS` emit to after slot acquisition**; add the deduplicated queued
  status past `notify_after_seconds`, **without** `COMPACTION_STATUS_MARKER` (§4.7).
- `conversation_loop.py`: `force_immediate=True` on the **numeric hard-wall condition** and
  after the wait-cap; **name the exact token-budget variable in the PR**; assert
  threshold-triggered compaction stays queued (§4.4.3).
- Per-session `first_pending_at` on the compressor (in-memory; loss on restart just re-arms
  the wait-cap).

### 9.4 Phase 3 — Observability + rollout
Emit §7 metrics/logs (including the resolved coordinator path); slot load in diagnostics.

**Ship DARK: `enabled: false`, `max_concurrent: 1` (rev 6).** Implementation lands with the
queue **off**. Turning it on is a **separate, explicitly user-approved step**, not part of
this build — the InstallDir *is* the running backend, so code that ships dark cannot change
live behaviour on restart. **Gate 0 (§9.0-GATE) and the cross-profile concurrency test (§9.6)
must both pass before `enabled: true` is even proposed.**

**Rollback:** `enabled: false` (pure config) → per-session-only behaviour; no migration to
undo. A stale `<root>/compaction.db` left behind is inert. **The §5 route guard stays on
regardless** — privacy control, not a performance one, and not part of the queue rollback.

### 9.5 Future work (only if metrics justify)
Priority scheduler via `compaction_waiters(session_id PK, source, priority, first_pending_at,
expires_at)`: waiters register on `DENIED`, refresh while pending; a releasing holder hands
off to the highest-priority non-expired waiter with a bounded hand-off window after which the
slot reverts to open acquisition (so a dead waiter cannot wedge the queue). Build only if
`wait_seconds{source=interactive}` shows real starvation.

### 9.6 Test strategy

**Unit (Phase 0):** acquire up to N; N+1th → `DENIED`; expired reclaim; idempotent release;
**injected `sqlite3.Error` → `COORDINATOR_ERROR`, never `DENIED`**; concurrent acquire across
two connections yields distinct `slot_id`s and respects the cap.

**Route guard (Phase 0.5) — the constraint-3 proof, re-anchored on the real path:**
- **Anthropic dual-route, OAuth present:** with a Claude Max OAuth source **and**
  `ANTHROPIC_API_KEY` set, assert `_try_anthropic` resolves to the **OAuth** credential
  (which `resolve_anthropic_token()` already prefers) **and** that the guard records
  `mode == "oauth_subscription"`.
- **Anthropic dual-route, OAuth ABSENT (the real hazard):** with **only** `ANTHROPIC_API_KEY`
  set, assert compaction is **rejected and fails closed** — **no Anthropic client is
  constructed, no request issued** — rather than silently using the metered key (today's
  behaviour, via `resolve_anthropic_token()` step 5).
- **Token-shape beats source-var, both directions** (§5.3.1): an `ANTHROPIC_TOKEN` holding an
  `sk-ant-api…` key → classified `api_key` → **not used**; a **legacy OAuth token parked in
  `ANTHROPIC_API_KEY`** (`sk-ant-`/`eyJ`/`cc-`) → classified `oauth_subscription` → **allowed**.
- **Mixed sources — scan-past (rev 5 / Codex r4 note):** set `ANTHROPIC_TOKEN` to a
  **metered-shaped** (`sk-ant-api…`) value **and** provide a valid **OAuth** source *later* in
  the precedence order (Claude Code credential store at step 3, or an `AUTH_TYPE_OAUTH`
  credential-pool entry at step 4). Assert compaction **succeeds on the later OAuth
  credential** — i.e. the resolver **did not stop at the short-circuiting step-1 token** the way
  `resolve_anthropic_token()` does — and assert the skipped candidate is counted
  (`credential_candidate_skipped_total{source=ANTHROPIC_TOKEN, reason=metered_shape}`) and that
  **no client was ever constructed against the metered value**.
- **Scan-past exhaustion:** every source metered-shaped (or absent) → **fail closed** (§5.5),
  with the skip counters non-zero *and* `routing_rejected_abort_total` incremented — confirming
  skip and rejection are distinguishable in telemetry.
- **`_try_anthropic(explicit_api_key=…)`** (from `:4881`) with a metered key → rejected.
- **Every rung independently** (primary `auto`, `auxiliary.compression.fallback_chain`,
  top-level `fallback_providers`, main-agent fallback): a metered candidate (OpenRouter /
  Gemini / `openai-api`) → **no client constructed, no request issued**.
- Allowlisted provider name aliased to a metered `base_url`/`api_key` → rejected by the
  constructed-client endpoint check (§5.3.4).
- **Config:** empty/malformed `allowed_routes` → **config error, not a guard-disable**; a
  metered route entry → rejected at load.

**`CompressionRoutingRejected` propagation (rev 4 / Codex r3 blocker 1) — new:**
- Raise `CompressionRoutingRejected` from inside the auxiliary call and assert it
  **propagates out of `_generate_summary`** — i.e. is **not** swallowed by the broad
  `except Exception` (`:2020`).
- Assert it **never** triggers `_fallback_to_main_for_compression` / a **main-model summary
  retry** (`:2142–2143`) — **no request is issued to the main agent model** (this is the
  second-egress regression test).
- Assert it does **not** set `_last_summary_error` and does **not** arm the generic
  summary-failure cooldown.
- Assert `compress()` aborts with `_last_compress_aborted = True` and returns messages
  unchanged, via the `_last_summary_routing_rejected` carve-out.
- **With `compression.abort_on_summary_failure: False` (the default)** assert: messages
  **unchanged**; **no static placeholder**; **no middle-window drop**; **`archive_and_compact`
  NOT called**; **no session rotation**; distinct routing-rejection message. Assert the same
  with the flag `True` — the flag must have **no effect** on a routing rejection.

**Multi-process, CROSS-PROFILE concurrency (the key queue test — rewritten rev 6):** this is
the test that would have caught the rev-5 bug, so its shape is not negotiable.

- **≥3 REAL subprocesses** (not threads, not mocks) under **one temp Hermes root**, spanning
  **≥2 distinct profiles**: e.g. one agent with `HERMES_HOME=<root>/profiles/a`, one with
  `HERMES_HOME=<root>/profiles/b`, one at `<root>` itself. Point the root at a temp dir via
  `HERMES_COMPACTION_HOME` (§6) so the developer's real `<root>/compaction.db` is untouched.
  **A same-profile-only test is insufficient** — it passes just as happily against the
  rejected profile-local substrate and would prove nothing.
- Assert all three resolve the **same** `compaction_db_path()` == `<root>/compaction.db`,
  **and** that their profile-local `state.db` paths **differ**. That contrast is the
  regression assertion for Gate 0; without it the test is tautological.
- Cross the threshold together; poll `get_compaction_slot_load` and assert **at most
  `max_concurrent`** slots are held at any instant **across profiles** (with
  `max_concurrent: 1`, a passing rev-5 implementation would show **2–3** here — that is the
  bug, made visible).
- All eventually compact; no deadlock; no messages lost (in-place `archive_and_compact`
  preserves archived rows).
- **Do not mock** `get_hermes_home` / `get_default_hermes_root` / path resolution: the defect
  class being guarded is *path resolution itself*.

**Fail-open:** coordinator raises → compaction still runs unbounded; numeric hard-wall bypasses
the slot; wait-cap escalates on the next re-check.

**Routing invariance (§5.1):** queue enabled vs disabled → identical arguments to and result
from `_resolve_task_provider_model("compression")`.

**No-throttle regression:** a `DENIED` session's turn/tool loop proceeds (messages unchanged,
**assert `on_pre_compress` NOT called**, no thread block); **assert no `COMPACTION_STATUS`
emit on a denied attempt** (§4.7); kanban dispatch cadence unaffected.

## 10. Failure modes

| Failure | Handling |
|---|---|
| Root coordinator DB (`<root>/compaction.db`) unavailable — unwritable root, corrupt/locked file, missing coordinator method (module/version skew), `AttributeError` at the call site | `COORDINATOR_ERROR` → **bypass queue, compact unbounded** (fail-open). Never `DENIED`. Log once/session; alertable. |
| **Coordinator resolves to an unexpected path** (e.g. a stray `HERMES_COMPACTION_HOME`, or a future refactor reintroducing an import-time constant) | **Silent by nature — this is the rev-5 failure class.** Every acquire succeeds and the bound quietly does nothing. Mitigated by Gate 0 (§9.0-GATE) + the cross-profile concurrency test (§9.6), and by logging the **resolved coordinator path** once per process at first acquire so an operator can see which file is actually in use. |
| Holder crashes mid-compaction | `expires_at` lease reclaimed by the next acquirer's `DELETE expired`. TTL 300s. |
| Stuck lease-refresher | Bounded ≤1-TTL give-up window — a slot cannot be held past TTL. |
| Starvation | Wait-cap bypass on the next re-check after `max_wait_seconds`. No ordering guarantee claimed (§4.6). |
| Thundering herd on release | Poll-on-natural-re-check, not wake-all; ≤ `max_concurrent` acquire per round. |
| Session nears hard wall while pending | Numeric hard-wall bypass (§4.4.3) — compact in place immediately. Never overflow, never `/new`. |
| `max_concurrent` 0 / negative | Clamped ≥1 at load. |
| Slot leaked | `expires_at` reclaims; bound self-heals within one TTL. |
| **Anthropic resolves to the metered API-key route (no OAuth source present)** | **Rejected by the route guard** (§5.3); `route_rejected_total{provider=anthropic, reason=metered_auth_mode}`; falls through to the next **allowed** route, else fail-closed. |
| **Routing rejection raised inside the aux call** | **Re-raised past `_generate_summary`'s broad handler** (§5.5); **never** retried on the main model (`:2142`); **not** recorded as a summary failure. |
| **All allowed subscription routes unavailable** | **Fail CLOSED** via `CompressionRoutingRejected`: messages unchanged, **no placeholder, no middle-window drop, no `archive_and_compact`, no rotation**, independent of `abort_on_summary_failure`. Session freezes at size with **full context intact**. |

Every *coordinator* failure degrades to today's per-session behaviour or immediate compaction
— never a block or deadlock. Every *routing* failure degrades to "no compaction this cycle,
context fully intact" — never an unapproved egress and never a silent context loss.

## 11. Triad review

**Rev 1 (Claude):** drafted against `8bb1d5696`.

**Codex r1 → `changes_required`** (fixed rev 2): typed slot result (denied vs
coordinator-error); priority overclaim withdrawn; subscription-only section added.

**Codex r2 (closure) → `changes_required`** (fixed rev 3): no empty-allowlist opt-out;
distinct fail-closed routing semantics; Anthropic auth-type mismatch raised.

**Codex r3 (source-fidelity closure) → `changes_required`.** Design accepted; two
source-fidelity blockers. Both accepted and fixed in rev 4:

| # | Blocker | Resolution (rev 4) |
|---|---|---|
| 1 | **`CompressionRoutingRejected` propagation under-specified** — `context_compressor._generate_summary` catches broad `Exception`, so the new signal could be converted into the generic summary-failure/static-placeholder path. | **Confirmed, and the exposure is worse than reported.** `_generate_summary`'s handler (`:2020`) not only funnels to the placeholder path — at **`:2142` it calls `_fallback_to_main_for_compression` and RETRIES THE SUMMARY ON THE MAIN MODEL (`:2143`)**, a **second routing escape** that could send compaction content to a metered main-agent route. **`agent/context_compressor.py` added to files-to-touch** (§9.1). §5.5 now requires: re-raise `CompressionRoutingRejected` **before** the broad handler; **never** reach `_fallback_to_main_for_compression`; **not** recorded as `_last_summary_error`; and `compress()` aborts unchanged via a new `_last_summary_routing_rejected` flag following the **existing** `_last_summary_auth_failure` / `_last_summary_network_failure` carve-out shape (`:1148–1163`), both already documented as *"independent of `abort_on_summary_failure`"* — **reuse, not invention**. Dedicated propagation tests added (§9.6), including a **no-request-to-main-model** assertion. |
| 2 | **Anthropic source anchoring wrong** — the compaction path is `_try_anthropic` → `resolve_anthropic_token()`, not `get_anthropic_key()`; the latter's metered-first order is not the compaction path. | **Confirmed — rev 3 was mis-anchored, and its headline hazard claim was FALSE.** Verified `resolve_anthropic_token()` (`anthropic_adapter.py:1274–1321`) prefers **`ANTHROPIC_TOKEN` → `CLAUDE_CODE_OAUTH_TOKEN` → Claude Code credentials → credential_pool → `ANTHROPIC_API_KEY` LAST** — OAuth **first**. Rev 3's "metered key wins first on compaction" alarm is **explicitly retracted** (correction notice + §3.3 + §5.2). All `get_anthropic_key`-based evidence replaced with `_try_anthropic` / `resolve_anthropic_token` evidence. **The real hazard is restated narrowly** (dual-route provider + genuine API-key fallback at step 5 + **zero provenance** returned). Resolver relocated: **`agent/anthropic_adapter.py`** (new `resolve_anthropic_token_with_provenance()`, reusing the **existing** `_is_oauth_token()` at `:386` as the authoritative classifier), consumed by **`agent/auxiliary_client.py::_try_anthropic`** (`:2610–2631`, incl. the `explicit_api_key` path at `:4881`) — the actual runtime compaction call path. **`hermes_cli/auth.py` is explicitly NOT changed** (§5.6 explains why, and files it as a separate diagnostics-fidelity bug). Also corrected: **token SHAPE, not source var, decides the mode** — `ANTHROPIC_API_KEY` may hold a **legacy OAuth token** and `ANTHROPIC_TOKEN` may hold an API key, so rev 3's source-var rule would have been wrong in both directions (§5.3.1, tested §9.6). |

**Codex r4 (third closure) → `approve_with_notes`. No blockers.** One nonblocking note,
resolved in rev 5:

| Note | Resolution (rev 5) |
|---|---|
| Anthropic provenance is source-faithful and privacy-safe, but the spec should say **whether compaction scans past an earlier metered-shaped token to a later OAuth token, or fail-closes immediately**. Either is safe; make the intended UX explicit. | **Policy: SCAN PAST** (§5.3.1). The compaction resolver walks the same precedence order but **does not stop at the first token found** — it skips metered-shaped candidates, continues, and returns the first `oauth_subscription` credential; it fails closed **only on exhaustion**. Adopted Codex's preferred policy, and found a **source-grounded reason it is required rather than merely preferable**: `resolve_anthropic_token()` **short-circuits** (`if token: return token` at each step, `anthropic_adapter.py:1290–1319`), so a metered-shaped value in `ANTHROPIC_TOKEN` (step 1) means **steps 3–4 are never reached** — fail-fast would let one stray env var **mask a valid Claude Max credential** and needlessly fail-close a user who genuinely has a subscription route. Privacy is unchanged (a metered token is skipped, never used). Also noted the credential pool's **structural** signal (`_resolve_anthropic_pool_token` already filters `auth_type == AUTH_TYPE_OAUTH`, `:1259–1261`), preferred over shape where present. Added `credential_candidate_skipped_total{provider, source, reason}` (§7) — **skip** (search continued) is deliberately distinct from **rejection** (route ended) — plus mixed-source and scan-past-exhaustion tests (§9.6). |

**Gemini:** **waived** (`IneligibleTierError`).

**Retraction (carried prominently, since it was escalated to the user):** rev 3's claim of a
live metered-egress hazard *by default* on the compaction path was **wrong**. Compaction
prefers OAuth. The corrected, narrower finding: **Anthropic compaction silently falls back to
the metered API key only when no OAuth/subscription source is present**, and no code today can
report which route was used. Phase 0.5 still closes this, and is still worth landing on its
own — but it is not the emergency rev 3 described.

**Next intended action:** Hermes verifies the doc-only diff, then routes Codex third closure
review. Stop for user approval before any implementation.
