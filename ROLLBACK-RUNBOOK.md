# Rolling back from the Codex chain-authority build (deploy gate F8)

This is the ONLY supported way to go from a build with the Codex chain authority (stage 1 + 2 of the
credential recovery protocol) back to the build deployed before it, `1b8cc8c1b5`, without any Codex
refresh token that Hermes already used being sent to OpenAI again, and without losing any other
provider's credentials.

## Why a backup is not a rollback

OpenAI Codex refresh tokens are single-use: every refresh consumes the old token and issues a new
pair. A presented consumed token is a *replay*; OpenAI answers `refresh_token_reused` and may revoke
the whole login family.

* Restoring an older `auth.json` (a backup, a quick snapshot, a profile export, a copy you made)
  puts back pairs the new build has since consumed. **Never do that, before or after a rollback.**
* Even without a restore, the rows the new build leaves behind are not all safe for the old build.
  The new build keeps its safety in files the old build never reads (`auth.chains.json`,
  `auth.recovery/`, `auth.ops/`, `auth.chains.retired.json`, `profiles/.locks/`). The old build reads
  every `auth.json` directly and would refresh:
  * a login **paused after an unconfirmed refresh** (`indeterminate`): its rows still hold the pair
    whose refresh token may already be consumed;
  * a **copy of a login in another profile**: only the owner's rows follow a refresh, so after one
    rotation the copy is a consumed pair;
  * a **replaced or removed login** (`retired`), a login paused for any other reason
    (`rejected`, `unexpected-token`, `legacy-rescue`, `unverified-history`, `ambiguous-lineage`,
    `op-unrecoverable`, ...), a row that is not registered, or a login whose last refresh was
    recorded but not yet written to its rows.

## The rules

1. Never restore an `auth.json` (or anything containing one) to roll back. Sign in again instead.
2. Prepare **with the new build**, and never start the old build until
   `hermes auth rollback-prepare --check` prints `SAFE TO ROLL BACK`.
3. Stop every Hermes process before preparing, and start nothing from the new build between the
   preparation and the switch.
4. Do not delete the new build's files (`auth.chains.json`, `auth.recovery/`, `auth.ops/`,
   `auth.chains.retired.json`, `auth.rollback-receipt.json`). The old build ignores them; rolling
   forward again needs them.

## Procedure

1. **Stop every Hermes process**: the desktop app, the gateway (its service / scheduled task), the
   relay dashboard task (`HermesRelayDashboard`), cron, and every CLI or TUI session.
2. **Optional preview** (read-only, no locks, writes nothing):

       hermes auth rollback-prepare --check

   It lists the Codex rows it would keep and remove, and exits non-zero while anything is left to do.
3. **Prepare** (new build):

       hermes auth rollback-prepare

   What you see and what to do:

   | output | meaning | do |
   |---|---|---|
   | `SAFE TO ROLL BACK. Receipt: <root>\auth.rollback-receipt.json` | done | step 4 |
   | `A Codex login update is unfinished ... finish it with ...` | an interrupted profile move / sign-in (nothing was removed) | run the command it names (`hermes auth recover --op <id>` or `--abandon`, or `hermes auth recovery resolve <id>`), then step 3 again |
   | `Another Hermes process is using this Codex login ...` | a process still holds a store lock | stop it, step 3 again |
   | `NOT SAFE TO ROLL BACK YET` + `Codex CLI login ...: holds a token Hermes ALREADY USED` / `can't check it` | the old build imports `$CODEX_HOME/auth.json` (default `~/.codex/auth.json`) whenever its own Codex login is missing or rejected | sign the Codex CLI in again (`codex login`), then step 3 again |
   | the command was interrupted / crashed | no receipt exists | run step 3 again (it is idempotent) |

   Sample of a successful run:

       Removed 3 Codex row(s) the old build must not use (it will ask you to sign in to Codex there):
           ...\profiles\work\auth.json  singleton  (fenced (indeterminate))
           ...\profiles\work\auth.json  pool:3f9a1c  (fenced (indeterminate))
           ...\profiles\side\auth.json  singleton  (copy outside the owner store)
       Codex login registry: ok
         kept 2 Codex row(s); the old build continues them:
           ...\auth.json  singleton  (login 5f0c2e...)
           ...\auth.json  pool:8d21aa  (login 5f0c2e...)
         Codex CLI login C:\Users\<you>\.codex\auth.json: none
         WARNING — under the old build these profiles have no Codex login of their own and would BORROW
         root's, ... sign in to Codex separately in each one you use Codex from (`hermes -p <name> auth`),
         or use Codex only from the default profile: side, work
       SAFE TO ROLL BACK. Receipt: ...\auth.rollback-receipt.json

4. **Re-verify right before the switch**: `hermes auth rollback-prepare --check` must print
   `SAFE TO ROLL BACK: nothing to prepare.` and exit 0. If anything ran in between (for example a
   process you missed paused a login), it lists what is left: go back to step 3.
5. **Switch the install to `1b8cc8c1b5`** (the same checkout/dependency steps as a deploy, in the
   other direction). Do not run any command of the new build after step 4.
6. **Start the old build and sign in where it asks**: every removed login needs a fresh sign-in,
   `hermes auth` for the default profile, `hermes -p <profile> auth` for a profile. Prefer the device
   / browser login; accept the "existing Codex CLI credentials" import only for a Codex CLI login made
   after step 3.
7. **Profiles in the WARNING line**: before using Codex from one of them, sign it in on its own
   (step 6), or use Codex only from the default profile (see Limits, 1).

## What the preparation does

Under the migration lock (the lock every ownership change of the new build takes):

1. Removes an earlier receipt, so an interrupted run never looks complete.
2. Runs the normal dispatcher pass (rolls decided ownership ops forward, archives undecided ones,
   pauses logins with legacy rescue files). If an op is still unfinished afterwards (a stuck profile
   move, an unrecoverable op) it **refuses**, names the command that resolves it, and removes nothing.
3. Settles every active login through the real refresh transaction **with the network disabled**
   (it is never forced and the token endpoint is replaced by a pre-send failure): a refresh the
   record shows as completed but not yet in the rows is written to the rows; an attempt of unknown
   outcome pauses the login (`indeterminate`).
4. Keeps a Codex row only if it is an **owner row of an ACTIVE login, holding exactly the pair its
   record names as current, with no refresh attempt in flight**. Every other Codex row (singleton or
   pool; any token material, even an access token alone) is removed through the same ownership op
   as `hermes auth remove`, one op per store: the login is retired, its record archived and its
   token hashes kept in the retired-token index, so the new build keeps recognising them. With an
   unreadable registry, or none although the new build has run here, every Codex row is removed
   (nothing can be vouched for). With no registry and no trace of the new build, nothing is touched.
5. Checks the Codex CLI login the old build would import: a pair Hermes has held (history or retired
   index) or cannot check blocks the receipt.
6. Re-reads every store under its lock and writes the receipt LAST, only if the result is safe.

Other providers' credentials, and every non-Codex part of each `auth.json`, are never changed.
Every write is one of the new build's own protocol writes (dispatcher, refresh transaction, removal
op, strict writer), so a crash at any point leaves a state the new build recovers by itself, and
running the preparation again completes it.

## Rolling forward again

Deploy the new build normally; do not restore anything. What it does with what the old build left:

* A login the old build did not touch stays active.
* A login the old build refreshed or re-signed-in (its rows no longer hold the pair the record
  names) is paused `unexpected-token` at its first refresh attempt, **before anything is sent**: sign
  in again with `hermes auth codex` (or `hermes -p <profile> auth codex`).
* A Codex row the old build created where the registry lists no row is reported as not registered
  and never refreshed; `hermes auth migrate` registers it — active only if Hermes never held its
  tokens and its evidence is complete, paused otherwise.
* A profile the old build renamed or deleted: `hermes auth migrate` retires the logins whose owner
  profile is gone; the rows at the new path are paused (`unverified-history`) until you sign in.

Measured in scenario f below: after the old build rotated a kept login once, the new build's startup
and a forced refresh sent nothing (`codex_recovery_fenced`, fence `unexpected-token`).

## Evidence

`scripts/codex_rollback_scenarios.py --old-tree <checkout of 1b8cc8c1b5>` runs the new and the old
build as separate processes against a throwaway root and a local recording token endpoint (single-use
tokens; every presented token is in its ledger). `tests/scripts/test_codex_rollback_scenarios.py`
runs it when `HERMES_TEST_ROLLBACK_OLD_TREE` names that checkout;
`tests/hermes_cli/test_codex_rollback_prepare.py` holds the unit tests.

| scenario | asserted |
|---|---|
| b0, c0 (controls) | WITHOUT the preparation the old build replays (the oracle detects the hazard) |
| a | active login rotated by the new build: the old build refreshes it (singleton and pool path) presenting only the latest token |
| b | login paused `indeterminate` (token consumed, response lost): the old build never presents it and asks for a sign-in |
| c | retired login with a consumed copy in a profile: the old build never presents the retired pair |
| d | unfinished op: preparation refused, nothing changed, no receipt; after `recovery resolve` it succeeds |
| e | a real process death (`os._exit`) at every step of the preparation: the new build stays safe on the crashed state, re-running the preparation completes it, then the old build is safe |
| f | roll forward after the old build rotated once: nothing sent, login paused `unexpected-token` |
| g | (reported) the old build alone, see Limits 1 |

Measured on commit `0f69342f2a` against a checkout of `1b8cc8c1b5` (full matrix, 226 s): zero
failures. Controls: b0 and c0 each replayed once without the preparation. a, b, d, f: zero replays.
c: zero replays up to the asserted step; the reported follow-on step (a root refresh after the
stripped profile borrowed root's login under the old build) replayed once — the Limits 1 path. e:
every one of the 11 crash points was reached (exit 137), left no receipt; the new build on the
crashed state and the re-run preparation then the old build: zero replays at every point. Reported:
the old build started directly on the crashed state replayed 3 times (crash before the settle), 1
time (crash after the settle, before the last removal op finished), 0 times (crash after all
removals). g: the old build alone replayed twice (root and a second profile after one profile
borrowed and refreshed).

## Limits — what this cannot make safe

1. **The old build's own borrow path.** In `1b8cc8c1b5` a profile with no Codex login of its own
   borrows the default profile's (`_load_provider_state` falls back to the root `auth.json`), and a
   refresh from that profile writes the new pair into the PROFILE (`_save_codex_tokens` saves the
   active store only), leaving root holding the consumed pair. The next refresh from root (or from a
   second borrowing profile) replays it. Scenario g reproduces this with the old build alone, no new
   build involved: it exists in the deployed build today. The preparation cannot change the old build;
   it removes a profile's own paused or copied login, which turns that profile into a borrower, and it
   therefore lists every profile that would borrow root's login (output WARNING line, receipt field
   `profiles_that_would_borrow_root`). Sign those profiles in on their own, or use Codex only from the
   default profile.
2. **Starting the old build on an unfinished preparation.** Until the preparation has completed, the
   rows it has not reached yet still hold what the new build left there. A crash never leaves a
   receipt and `--check` stays non-zero, but nothing stops someone from starting the old build anyway.
   Scenario e reports what the old build does when started directly on each crashed state.
3. **Anything running after the check.** A new-build process still running after step 4 can pause a
   login (its rows then hold a possibly consumed pair). Stop everything first; `--check` right before
   the switch is the gate.
4. **The Codex CLI file** is checked when the preparation runs. If the Codex CLI and Hermes share one
   login (`holds the same login Hermes keeps`), the old build and the CLI can both refresh it — as
   before the new build existed.
5. **Backups.** The old build's restore commands (backup import, quick-snapshot restore, profile
   import) put back whatever `auth.json` they contain. Rule 1.
