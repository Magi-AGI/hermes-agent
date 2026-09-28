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
* The old build also has hazards of its own that the new build's state can trigger: a second row of
  one login (a `manual:device_code` alias) is refreshed and then replayed through the singleton; a
  profile without its own Codex login BORROWS the default profile's, and its refresh leaves the
  default profile holding a consumed pair; a same-account profile login is merged into the default
  profile's by its fork heal (making the profile a borrower); and a missing, token-less or rejected
  login makes it import the Codex CLI's `auth.json` — into several homes at once.

## The rules

1. Never restore an `auth.json` (or anything containing one) to roll back. Sign in again instead.
2. Prepare **with the new build**, and never start the old build until
   `hermes auth rollback-prepare --check` prints `SAFE TO ROLL BACK`. The check reads the live state
   (not the receipt); it is the gate.
3. Stop every Hermes process before preparing, and start nothing from the new build between the
   preparation and the switch.
4. Do not delete the new build's files (`auth.chains.json`, `auth.recovery/`, `auth.ops/`,
   `auth.chains.retired.json`, `auth.rollback-receipt.json`). The old build ignores them; rolling
   forward again needs them.
5. **While on the old build, use Codex from the default profile** (and from profiles the
   preparation kept with a login of ANOTHER account). The preparation switches Codex OFF for the old
   build in every other profile. Do not sign in to Codex in such a profile with the default
   profile's account, and do not use Codex from a profile created while on the old build: the old
   build merges or borrows those and replays.
6. Keep the Codex CLI's login out of reach of the old build (`codex logout`, or move
   `$CODEX_HOME\auth.json` / `~\.codex\auth.json` aside) for as long as you run the old build.

## Procedure

1. **Stop every Hermes process**: the desktop app, the gateway (its service / scheduled task), the
   relay dashboard task (`HermesRelayDashboard`), cron, and every CLI or TUI session.
2. **Optional preview** (read-only: plain reads, no locks, writes nothing):

       hermes auth rollback-prepare --check

   It lists what it would keep, remove and switch off, and exits non-zero while anything is left.
3. **Prepare** (new build):

       hermes auth rollback-prepare

   What you see and what to do:

   | output | meaning | do |
   |---|---|---|
   | `SAFE TO ROLL BACK. Receipt: <root>\auth.rollback-receipt.json` | done | step 4 |
   | `A Codex login update is unfinished ... finish it with ...` | an interrupted profile move / sign-in (nothing was removed) | run the command it names (`hermes auth recover --op <id>` or `--abandon`, or `hermes auth recovery resolve <id>`), then step 3 again |
   | `Another Hermes process is using this Codex login ...` | a process still holds a store lock | stop it, step 3 again |
   | `Hermes can't inspect ...` / `... is not valid JSON ...` | a home could not be listed or read, so the inventory is not provably complete | fix access, or repair / move that file, then step 3 again |
   | `The Codex login registry is damaged ...` | the registry is the only record of homes outside `profiles/` | `hermes auth migrate --rebuild`, then step 3 again |
   | `... was damaged or removed after this build used it ... --other-homes-checked` | a rebuilt or missing registry may not list every home | remove the Codex login from any other Hermes home you ran under this root (a `HERMES_HOME` outside `profiles/`), then `hermes auth rollback-prepare --other-homes-checked` |
   | `NOT SAFE ...` + `Codex CLI login ...: holds a login the older build would IMPORT` | rule 6 | `codex logout` (or move the file aside), then step 3 again |
   | the command was interrupted / crashed | no receipt | run step 3 again (it is idempotent) |

   Sample of a successful run (multi-profile install):

       Removed 4 Codex row(s) the older build must not use:
           ...\profiles\work\auth.json  singleton  (the older build would merge this login into the default profile's (same or unknown account))
           ...\profiles\work\auth.json  pool:3f9a1c  (the older build would merge this login ...)
           ...\profiles\side\auth.json  singleton  (copy outside the owner store)
           ...\auth.json  pool:manual  (an alias the older build syncs from the singleton but never writes back)
       Codex login registry: ok
         kept 3 Codex row(s); the older build continues them:
           ...\auth.json  singleton  (login 5f0c2e...)
           ...\auth.json  pool:8d21aa  (login 5f0c2e...)
           ...\profiles\other\auth.json  singleton  (login 91ab07...)
         Codex is OFF for the older build in these homes (...):
           ...\profiles\notes\auth.json
           ...\profiles\side\auth.json
           ...\profiles\work\auth.json
         Codex CLI login C:\Users\<you>\.codex\auth.json: none
       SAFE TO ROLL BACK. Receipt: ...\auth.rollback-receipt.json

4. **Re-verify right before the switch**: `hermes auth rollback-prepare --check` must print
   `SAFE TO ROLL BACK: nothing to prepare.` and exit 0. If anything ran in between, it lists what is
   left: go back to step 3.
5. **Switch the install to `1b8cc8c1b5`** (the same checkout/dependency steps as a deploy, in the
   other direction). Do not run any command of the new build after step 4.
6. **Start the old build.** A removed login of the default profile needs a fresh sign-in
   (`hermes auth`, browser / device login). Profiles with Codex OFF answer "No Codex credentials
   stored" and send nothing; keep using Codex from the default profile (rule 5).

## What the preparation does

Under the migration lock (the lock every ownership change of the new build takes):

1. Removes an earlier receipt, so an interrupted run never looks complete.
2. Takes a STRICT inventory: the root, the active home, every directory under `profiles/` (the
   listing itself and the profile enumeration may not fail), and every home the registry names;
   every `auth.json` is parsed from its raw bytes. Anything it cannot inspect, an unreadable
   registry, or (without `--other-homes-checked`) a registry that was damaged or removed after use,
   REFUSES: a damaged registry cannot prove it knows every home outside `profiles/`.
3. Runs the normal dispatcher pass. An op still unfinished afterwards REFUSES (nothing removed).
   With no registry yet (the new build never ran here) but Codex material present, that pass's
   bootstrap migration first registers every Codex row by its token material across ALL homes:
   identical pairs anywhere are one login, owned by the default profile when it holds a copy;
   ambiguous rows are paused (and so removed below). The keep rules then apply exactly as for a
   governed install; `--check` refuses to certify such an install until it has been prepared.
4. Settles every active login through the real refresh transaction **with the network disabled**
   (never forced; the token endpoint is replaced by a pre-send failure).
5. Keeps a Codex row only if it is an owner row of an ACTIVE login holding exactly the pair its
   record names as current, with no attempt in flight; then, per home, only what the old build
   keeps in step (the singleton plus at most one `device_code` row; one row for a login without a
   singleton; no `device_code` / `manual:device_code` row of another login beside a singleton);
   and not a profile login the old build's heal would merge into the default profile's (kept only
   when both sides carry a known, different account and share no row id or token).
6. Records the token hashes of EVERY row it removes (registered or not, with or without an id) in
   the retired-token index, then removes them: a login losing every row is retired like
   `hermes auth remove`; a login keeping one row only drops the others; id-less rows directly.
7. Switches Codex OFF for the old build in every home other than the default profile that keeps no
   login of its own: an EMPTY `providers.openai-codex` section (the old build reads it as "no login":
   no fallback to the default profile, no Codex CLI import, ignored by its heal) plus one inert,
   key-less pool row `manual:rollback-codex-off` (no pool fallback; never leased or refreshed). A
   token-less provider section in the default profile (a CLI-import trigger) is dropped.
8. Requires that no importable Codex CLI pair exists, re-reads every home under its lock, and writes
   the receipt LAST, only if the result is safe.

Other providers' credentials, and every non-Codex part of each `auth.json`, are never changed. Every
write is one of the new build's own protocol writes (dispatcher, refresh transaction, ownership op,
strict writer), so a crash leaves a state the new build recovers by itself, and running the
preparation again completes it. Only an install with no Codex material at all needs no
registration (profiles still get Codex OFF).

## Rolling forward again

Deploy the new build normally; do not restore anything. What it does with what the old build left:

* A login the old build did not touch stays active.
* A login the old build refreshed or re-signed-in (its rows no longer hold the pair the record
  names) is paused `unexpected-token` at its first refresh attempt, **before anything is sent**: sign
  in again with `hermes auth codex` (or `hermes -p <profile> auth codex`).
* A profile with Codex OFF reads as "no Codex login" in the new build too: sign it in
  (`hermes -p <profile> auth codex`) to use Codex there again.
* Every removed pair stays in the retired-token index: importing it is refused, registering it
  fences it.
* A Codex row the old build created where the registry lists no row is reported as not registered
  and never refreshed; `hermes auth migrate` registers it — active only if Hermes never held its
  tokens and its evidence is complete, paused otherwise.
* A profile the old build renamed or deleted: `hermes auth migrate` retires the logins whose owner
  profile is gone; the rows at the new path are paused (`unverified-history`) until you sign in.

## Evidence

`scripts/codex_rollback_scenarios.py --old-tree <checkout of 1b8cc8c1b5>` runs the new and the old
build as separate processes against a throwaway root and a local recording token endpoint (single-use
tokens; every presented token is in its ledger). `tests/scripts/test_codex_rollback_scenarios.py` and
`tests/scripts/test_codex_rollback_old_paths.py` (the F8 review's reproductions: a home hidden by a
damaged registry, one CLI pair importable into empty and token-less profiles, aliases cycled
pool-first and singleton-first, the multi-profile workload) run them when
`HERMES_TEST_ROLLBACK_OLD_TREE` names that checkout; `tests/hermes_cli/test_codex_rollback_prepare.py`
and `tests/hermes_cli/test_codex_rollback_review_edges.py` hold the in-process tests.

| scenario | asserted |
|---|---|
| b0, c0 (controls) | WITHOUT the preparation the old build replays (the oracle detects the hazard) |
| a | active login rotated by the new build: the old build refreshes it (singleton and pool path) presenting only the latest token |
| b | login paused `indeterminate` (token consumed, response lost): the old build never presents it and asks for a sign-in |
| c | retired login with a consumed copy in a profile: never presented; that profile (Codex OFF) and the default profile cycled back to back: no replay |
| d | unfinished op: preparation refused, nothing changed, no receipt; after `recovery resolve` it succeeds |
| e | a real process death (`os._exit`) at each of 12 preparation steps: the new build stays safe on the crashed state; re-running the preparation, then the old build across the homes: no replay |
| f | roll forward after the old build rotated once: nothing sent, login paused `unexpected-token` |
| g | (reported) the old build alone: its own borrow path replays |
| h | multi-profile workload (Codex-OFF borrowers, a same-account profile login, a copy, another account) cycled 18 steps across homes and both paths: no replay; both kept logins keep refreshing |
| i | no registry and no trace of the new build, a singleton + `manual:device_code` alias of one pair in the default home and a copy in a profile: `--check` refuses; the preparation registers by token material; the old build cycled on both paths: no replay |

Measured on the commit that adds this text, against a checkout of `1b8cc8c1b5` (full matrix, 387 s):
zero failures. Controls b0, c0: one replay each without the preparation. a, b, c, d, f, h, i: zero
replays (h: 11 successful refreshes across the default profile and the other-account profile; every
Codex-OFF profile answered `codex_auth_missing` and sent nothing; i: `--check` refused before the
preparation, then 5 successful refreshes from the default home). e: all 12 crash points reached
(exit 137), no receipt left, zero replays after the re-run. Reported, the old build started DIRECTLY
on a crashed state (rule 2 forbids it): 8 replays (crash before the settle), 3–4 (after the settle,
before the removals / Codex-OFF switch finished), 0 (crash after all of them). g: the old build
alone, two replays.

## Limits — what this cannot make safe

1. **Using the old build outside rule 5.** The preparation leaves a state in which the old build
   cannot replay; the old build's own borrow path and fork heal remain in it (scenario g). Signing a
   Codex-OFF profile in with the default profile's account, or using Codex from a profile created
   under the old build, re-creates that hazard. Codex from the default profile (and from a profile
   with a login of ANOTHER account) is safe.
2. **Starting the old build before the preparation completed** (rule 2); scenario e measures it. A
   crash always leaves no receipt, but a crash after the last write and before the receipt can
   already leave a safe state; `--check` judges the live state, not the receipt.
3. **Anything running after the check** (rule 3). A new-build process still running after step 4 can
   pause a login; `--check` right before the switch is the gate.
4. **The Codex CLI after the switch** (rule 6). Once the CLI signs in again, the old build imports its
   pair into any home whose login is missing or rejected — as it did before the new build existed.
5. **Backups** (rule 1). The old build's restore commands put back whatever `auth.json` they contain.
6. **Homes outside `profiles/` after a damaged registry.** `--other-homes-checked` is your
   confirmation; the preparation cannot find such a home without the registry.
