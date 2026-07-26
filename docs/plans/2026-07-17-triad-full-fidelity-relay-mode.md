# Triad Relay Pipe, Lean Artifacts, and Manual-Review Gates — Gate 0

- **Date:** 2026-07-17 (drafted 2026-07-18; path kept per the originating packet)
- **Workstream:** `hermes-full-fidelity-relay-mode` · **Checkpoint:** CP013
- **Base:** `lake/desktop-session-window-qol` @ `7e84dfb5f2bdfdbe9d98950ccbab4cdffb4317b8`
- **Mode:** implemented (CP015–CP022); this document is the design of record.
- **Scope (Lake-accepted, CP012):** **D1-pipe + D3 + D4.** D2 posture deferred to the
  next Gate 0; D2 enforcement and D1 forensics parked (§7).
- **Triad:** Claude lead/write · Codex `approve_with_notes` on the retaxonomy (CP011)
  · agy: no verdict, quota-exhausted in all eight prior rounds.

## 1. Goal

Three small, mutually reinforcing fixes for the drift Lake observed in deck and code
review:

| | Defect | Fix |
|---|---|---|
| **D1** | Full responses are summarized in transit because Hermes reads and re-emits them | Capture once to a file; relay a **path**, not content |
| **D3** | Drafting docs and checkpoints consume the context reviewers need for the artifact | Lean artifact policy + handle-based packets |
| **D4** | Artifacts get locked before Lake has personally tested them | Explicit three-state decision model |

D1 and D3 produce the same symptom by different routes — **transit loss** vs
**post-arrival compaction**. Fixing the pipe alone does not fix the budget, which is
why both ship together.

## 2. D1 — the relay pipe

### 2.1 Problem

Relaying a response today means Hermes reads it into its own context and re-emits it
into the next agent's prompt. That summarizes under pressure, doubles token cost, and
fills Hermes' context — and Hermes compacting is itself a source of relay drift.

### 2.2 Token model (precise)

**What becomes near-zero-token:** Hermes' pass-through. Hermes handles a ~20-token
path instead of the full body, so the content never enters its context and never gets
re-emitted.

**What still costs tokens:** the receiving model reading the artifact. Any content a
model reasons about must be in its context. There is no way around that, and this spec
does not claim otherwise.

Illustrative, for a 5k-token response:

| | Today | With the pipe |
|---|---|---|
| Author generates | 5k | 5k |
| Hermes reads | 5k | ~0 |
| Hermes re-emits into next prompt | 5k | ~0 |
| Receiver reads | 5k | 5k |
| **Total** | **~20k** | **~10k** |

The eliminated half is also the half that pollutes Hermes' context.

### 2.3 Minimal surface

New `hermes_cli/relaypipe.py` + subparser wiring (`hermes_cli/main.py`, following the
`checkpoints`/`curator` pattern at `:13121`/`:13226`; add to `_BUILTIN_SUBCOMMANDS` at
`:12283`).

```
hermes handoff capture [--session <id>] [--message-id <id>] [--last]
                       [--workstream <name>] [--label <name>] [--dir <path>]
    # Writes the selected assistant response (default: last) to
    # <dir>/<label>.md and prints ONE line: the absolute path.

hermes handoff list [--session <id>]     # recent artifacts, newest first
hermes handoff show <path>               # print an artifact (human/debug use)
```

**Addressing defaults (deterministic, no guessing):**

| Argument | Omitted behavior |
|---|---|
| `--session` | most recently active session **in the active profile**, resolved via `SessionDB.list_sessions_rich` (`hermes_state.py:2998`) ordered by last message timestamp. **The resolved session id is echoed to stderr**, so a wrong pick is visible immediately and never silent. |
| `--workstream` | falls back to `triad.workstream` in `config.yaml`; if that is empty, **exit non-zero with a clear message**. No invented slug — a standalone CLI guessing a workstream would write artifacts somewhere nobody looks. |
| `--message-id` | **a SessionDB `messages.id` row id** (`hermes_state.py:748–768`). Captures **assistant rows only**; a non-assistant id exits non-zero unless a future flag explicitly widens it. |
| `--last` | most recent assistant row in the resolved session. |

**Byte-identical capture reads the DB row directly** — *not* `sessions export`. Export
runs through rendering, and `security.redact_secrets` is bridged to a process-global
`HERMES_REDACT_SECRETS` that `agent.redact` snapshots at import
(`hermes_cli/main.py:537–541`), so an export-based capture could silently emit redacted
text while reporting success. `sessions export` (`main.py:13615`) stays useful
precedent for CLI shape. `SessionDB.get_messages` (`:3747`) selects the row
(role/id validation), but the **bytes come from the raw `messages.content`
column** — `get_messages` runs content through `_decode_content` (`:3428`), which
turns a stored payload back into an object. Structured content is persisted
behind the NUL sentinel `_CONTENT_JSON_PREFIX = "\x00json:"` (`:3403`) plus
`json.dumps(content)` with default separators, so re-serializing the decoded
object would emit different bytes
than the row holds. Plain strings are unaffected; this only matters for
structured assistant content, where it is the difference between verbatim and
close enough — at the cost of a literal NUL byte in such artifacts.

**Exact text preservation:** UTF-8 in, UTF-8 out; no newline translation, no reflow, no
Markdown rendering, no trailing-whitespace normalization. Write with newline
translation disabled so CRLF in a source survives on Windows.

**Naming:** `handoff`, not `relay` — `relay` is taken by the gateway relay connector
(`gateway/relay/*`, `Platform.RELAY` at `gateway/config.py:243`, `GATEWAY_RELAY_*`).

### 2.4 Reviewer seeding

Reviewer prompts carry the path, not the body:

```
Read the full source at:
  C:/Users/Lake/AppData/Local/hermes/profiles/claudetriad/triads/<ws>/handoff/cp013-claude.md
Review it directly. Do not rely on any summary in this prompt.
```

### 2.5 Artifact location — recommended default (Gate 0 decision)

Codex asks for a decision here rather than an open question, because reviewer-seed
implementation depends on it. **Recommendation, which reverses my CP012 lean:**

> **Default to a workspace-adjacent `.triad/` directory** — resolved as the git root,
> else cwd — **gitignored**. `triad.handoff_dir` in `config.yaml` and `--dir` override
> it absolutely; set either to `<HERMES_HOME>/handoff/` for profile isolation.
>
> **CONFIRMED by Lake (CP015).** Implemented; `.triad/` is in the repo `.gitignore`.

**The trade-off, stated plainly:**

| | `<HERMES_HOME>/handoff/` | workspace `.triad/` (recommended) |
|---|---|---|
| Profile isolation | native | needs the config override |
| Reviewer readability | requires a correct working-dir grant on **every** lane | works with zero configuration when the lane's cwd is the workspace |
| Failure mode | **silent** — unreadable path, reviewer reviews nothing, reports confidently | loud — a missing file is an obvious error |
| Non-repo work (decks) | fine | falls back to cwd; set `handoff_dir` for a stable location |

The decisive factor is the failure mode. An unreadable handoff path produces exactly
the defect this workstream exists to remove: a reviewer that silently reviews nothing.
Requiring three lanes to each carry a correct directory grant is fragile in the one
place we cannot afford fragility. **This remains Lake's call** (§6 Q1) — Codex asked
Lake directly — but shipping without a default would block the seed work.

**Either way, the grant story is mandatory, not optional.** For Claude Code that is
`--add-dir <artifact-dir>` when the dir is outside cwd; Codex and agy need their
equivalent. Seeds must run the §2.6 smoke test before review begins.

### 2.6 Acceptance tests

1. `capture --last` writes the last assistant message **byte-identical to the raw
   `messages.content` column** — not to an export, and not to `get_messages`' decoded
   view. Includes a case with `security.redact_secrets` **on**, and a structured
   (list) content case proving the stored bytes are captured rather than a
   re-serialization.
2. Exact-text preservation: CRLF, trailing whitespace, and non-ASCII survive a
   round-trip unchanged; no Markdown rendering.
3. `capture --message-id` resolves a SessionDB row id; a **non-assistant** row and an
   out-of-range id each exit non-zero.
4. `--session` omitted resolves the most recently active session **and echoes the
   resolved id to stderr**; `--workstream` omitted with no `triad.workstream` config
   exits non-zero.
5. `capture` prints exactly one line on stdout (the absolute path) — safe to embed in
   a prompt without parsing.
6. **Seed smoke test:** a seeded lane opens the handed-off path *before* review begins
   and fails loudly (non-zero) if it cannot, rather than proceeding to review nothing.
7. **Hermes does not inline the body.** Assert the relay message Hermes emits contains
   the path and contains **no 64-character contiguous substring of the artifact body**.
   Deterministic, and directly tests the property we care about; a token-count delta is
   noisier and harder to attribute.
   **Plus two variants, so the 64-char rule cannot give false confidence:** (a) a
   **short artifact** (< 64 chars) — the substring rule is vacuous there, so assert
   instead that the emitted message length stays within a small bound over the path;
   (b) a **repetitive artifact** (e.g. a long run of boilerplate) — assert the emitted
   message is not merely *shorter* than the body but shares no distinctive span,
   guarding against a partial inline that happens to dodge one 64-char window.
8. **Artifact dir resolution:** with no override, artifacts land in workspace-adjacent
   `.triad/` (git root, else cwd); with `triad.handoff_dir` or `--dir` set, they land
   there instead and the override wins over the workspace default. Both cases resolve
   against the **active profile's** config.

## 3. D3 — context budget and lean artifacts

### 3.1 Problem

Reviewers compact mid-task because process artifacts crowded out the work. This spec's
own history is the worked example: nine revisions, 637 → 924 lines, eight accumulated
correction notices, plus nine checkpoint packets and nine resolution results, each
round re-reading a growing document.

**Status:** Codex flags "D3 is dominant" as a **working diagnosis, not a settled
root-cause claim** (CP011). Adopted as written — D3 is treated as high-probability and
cheap to fix, not proven.

### 3.2 Policy

1. **One living document per workstream**, revised in place. No per-round docs.
2. **Target ~250 lines; hard warning at 400.** Over 400 requires explicit written
   justification in the resolution.
3. **Correction notices are transient** — fix the text, record a one-line changelog
   entry, drop the notice next revision. History belongs in git.
4. **Checkpoint packets carry handles, not embedded bodies.** The current template
   inlines full source bundles; that is the bloat pattern, and CP012 already switched
   to source handles.
5. **Resolution results carry decisions and deltas**, never a restatement of the spec.
6. **Reviewer seeds point at paths** (§2.4).

### 3.3 Enforcement surface

```
hermes handoff lint [--path <doc>] [--strict]
    # Warns over the line budget, on embedded source bundles that could be
    # handles, and on stale correction notices.
```

**Two modes, deliberately:** plain `lint` is **advisory** (exit 0) for local editing,
where a hard failure mid-draft is just friction. **`--strict` (exit non-zero) is
mandatory in reviewer, CI, and pre-dispatch workflows** — the points where an
over-budget artifact is about to consume a reviewer's context. A warning nobody is
required to act on is a warning that gets ignored; the strict mode is what gives the
policy teeth at the moment it matters.

### 3.4 Acceptance tests

1. `lint` warns above 400 lines and is silent at 250.
2. `lint` flags a packet embedding a large verbatim body where a path would do.
3. `lint --strict` exits non-zero on a violation; plain `lint` exits 0.
4. Templates updated: packet and resolution templates default to handles.

## 4. D4 — manual-review gates

### 4.1 Problem

Three distinct states are being collapsed into "locked," and the collapse happens
before Lake has tested the artifact.

### 4.2 The three-state model

| State | Trigger | Locks? |
|---|---|---|
| **`proposed`** | An agent recommends a decision | **No** — non-binding |
| **`acknowledged`** | Lake responds approvingly without full review ("sounds good") | **No** — still fluid |
| **`locked`** | **Lake performs a full-artifact manual review event** | **Yes** |

**A lock requires reviewing the whole artifact, not a description of it:**

- **Deck:** Lake has viewed the **complete deck**, not a slide summary or a change
  list.
- **Code:** Lake has **executed the whole program**, not read a diff or a checkpoint.

The distinction that matters: **`acknowledged` is currently being treated as
`locked`.** An agent proposes a freeze, Lake says "sounds good" to the description,
and downstream work builds on a decision Lake has never actually tested. That is the
defect.

### 4.3 Minimal surface

A per-workstream `decisions.jsonl` under the handoff dir, one line per decision:

```jsonc
{"id":"deck-slide4-layout","status":"proposed","statement":"...",
 "proposed_by":"claude","at":"2026-07-18T…",
 "review_event":null}
```

`review_event` is populated only on a Lake manual review, recording what was reviewed
(`"full deck read 2026-07-18"`, `"executed hermes --tui end-to-end"`).

```
hermes handoff decide <id> --status proposed|acknowledged|locked
                            [--review-event <text>]
    # --status locked REQUIRES --review-event; refuses otherwise.
```

**Doctrine carries most of the weight.** Skills and reviewer seeds must render
decision status explicitly and must never treat `proposed` or `acknowledged` as
authoritative. The file makes the state checkable; the doctrine makes it respected.

### 4.4 Acceptance tests

1. `decide --status locked` **without** `--review-event` exits non-zero.
2. **`proposed` → `locked` in one step is allowed when `--review-event` is supplied.**
   The lock requirement is a recorded Lake review event, **not** a prior
   `acknowledged` status — Lake may review a fresh proposal directly, and forcing an
   intermediate would be ceremony that teaches people to click through it.
3. Reviewer seeds render status inline, so a reviewer cannot mistake `acknowledged`
   for `locked`.
4. Worked deck example: a slide decision stays `proposed` through agent
   recommendation, becomes `acknowledged` on Lake's approving reply, and only reaches
   `locked` after a recorded full-deck review.
5. Worked code example: same lifecycle, with `locked` gated on a recorded whole-program
   execution.

## 5. Implementation order

1. **D1 capture + list + show** — the pipe, reusing `sessions export` / `get_messages`.
2. **D4 `decisions.jsonl` + `decide`** — tiny, and actively costing Lake now.
3. **D3 `lint` + template updates** — advisory.
4. **Seeds and skills** — handles, working-dir grants, decision status rendering.

Steps 1–3 are independent and separately revertible. Nothing here touches
`run_agent.py`, `conversation_loop.py`, `hermes_state.py` schema, the plugin system,
ACP, the API server, the TUI, or Desktop.

## 6. Open questions for Lake

**Settled (Lake, CP015):**

1. **Artifact location — workspace-adjacent `.triad/`** (git root, else cwd),
   gitignored, with `triad.handoff_dir` and `--dir` overriding. Chosen because an
   unreadable path fails *silently*.
2. **A manual review event is explicit natural language naming the reviewed
   artifact** — "read the full deck", "ran the program end to end". Deliberately not a
   keyword, so it cannot fire accidentally.
3. **Line budget: ~250 target, 400 hard warning.**
4. **Default session** — most recently active, with the resolved id echoed to stderr
   (§2.3), so an implicit pick is never silent.
5. **Relay v1 selectors** — `--last`, `--message-id`, and `--session` all ship
   (Codex CP011 Q1).

**Still open:**

- None blocking implementation. Templates and reviewer seeds (§3.4 test 4, §4.4
  test 3) live in the profile, not this repo, and are the remaining step-4 work.

## 7. Deferred — pointers only

- **D2 prompt posture** (suppress the conflicting core guidance blocks that push GPT
  models toward autonomous solving — `system_prompt.py:205–206`, `:216–217`,
  `:263–278`, `:282–290`, `:350–363`). Small and useful. **Next Gate 0 after this one.**
- **D2 runtime enforcement** (Lake-facing output contract). Whole-product refactor
  across callbacks, plugin hooks and middleware, ACP, API server, TUI, Desktop, and
  every result path. **Parked.** The surface inventory from CP001–CP008 is preserved in
  git history for whoever picks it up.
- **D1 forensics** (capture classes, tamper-resistant provenance). **Parked** — it
  defends against a malicious local agent, which is not a threat Lake has described.

## 8. Changelog

| Rev | Change |
|---|---|
| 1–8 | Grew to 924 lines covering D1 packet-provenance + D2 enforcement across eight Codex rounds; each round found a new egress surface. |
| 9 | Recommended splitting D1 from D2 after the D2 surface proved to span every output path in Hermes. |
| CP011 | Lake re-scoped: the ask was a **relay pipe**, not a provenance system; added **D3** (context budget) and **D4** (premature freeze). Codex `approve_with_notes`. |
| **CP012** | **Rewritten to D1-pipe + D3 + D4 only.** Provenance machinery, D2 enforcement detail, and accumulated correction notices removed — they are in git history. |
| **CP013** | Narrow revision for Codex `changes_required`: deterministic addressing defaults (§2.3); artifact location recommendation reversed toward workspace `.triad/` on failure-mode grounds (§2.5); D4 lock gated on a review event rather than a prior status (§4.4); capture reads the DB row directly, not `sessions export`; `lint --strict` mandatory pre-dispatch. |
| **CP015–CP017** | Implemented as `hermes handoff capture\|list\|show\|lint\|decide\|decisions` (`hermes_cli/relaypipe.py`) with 24 tests. Lake's three defaults confirmed and marked. Two bugs found only by running it: test DB isolation (`SessionDB()` ignores a redirected `HERMES_HOME`), and the top-level CLI dispatcher discarding subcommand exit codes — which had silently defeated `lint --strict` for every command using that convention. |
