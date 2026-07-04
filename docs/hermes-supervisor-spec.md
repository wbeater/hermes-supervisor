# Hermes Supervisor External Contract Spec

Status: Proposed
Owner: architect
Last updated: 2026-07-03
Source inputs:
- `<companion-project>/docs/hermes-gateway-supervisor-design.md`
- current prototype workspace at `<repo-root>`

## 1. Purpose

This document turns the gateway supervisor design note into an implementation-ready specification for an external Hermes supervisor/plugin.

The supervisor must:
- remain outside Hermes core
- lazy-start a profile gateway when needed
- idle-stop non-default profile gateways after inactivity
- perform a periodic safety sweep for leaked or orphaned process trees
- expose a stable CLI contract that backend implementation can follow without further design decisions

This spec defines the external contract only. It does not require or authorize any Hermes core source changes.

## 2. Non-goals

The supervisor does not:
- modify Hermes core runtime logic
- replace Hermes gateway implementation details
- guarantee memory recovery for processes it did not start or cannot attribute safely
- add a new scheduling system inside Hermes core
- manage unrelated user processes outside the supervisor-owned process inventory

## 3. Design decisions

1. External boundary
   - All supervisor code lives in `<repo-root>`.
   - All runtime state, locks, pid metadata, and logs live under `~/.hermes/supervisor/`.
   - Optional plugin registration, if Hermes supports it, lives under `~/.hermes/plugins/gateway-supervisor/` and points back to the external workspace.

2. Implementation language
   - Python 3 stdlib only unless a later ADR explicitly approves extra dependencies.
   - Rationale: matches the current prototype shape and keeps deployment simple on macOS/Linux.

3. State store
   - SQLite at `~/.hermes/supervisor/state.db` is the source of truth.
   - Rationale: current workspace already contains a SQLite placeholder; SQLite gives atomic updates, concurrent readers, and an audit log without requiring an external service.

4. Process ownership model
   - The supervisor only manages gateways started through its own `gateway start` contract.
   - Every started gateway must run in its own process group/session and carry supervisor ownership metadata.
   - Sweep must never kill a process unless ownership is proven by state plus runtime metadata.

5. Idle policy defaults
   - Non-default profiles: idle timeout = 900 seconds.
   - Default profile: exempt from automatic idle-stop by default.
   - Safety sweep interval = 1800 seconds.
   - Reaper poll interval = 60 seconds.

6. Control philosophy
   - `gateway start` is idempotent and doubles as the activity-touch boundary.
   - `gateway stop` is idempotent and safe to call on already-stopped gateways.
   - `reap` is policy-driven cleanup of known gateways.
   - `sweep` is conservative cleanup of proven-orphaned supervisor-owned process trees.

## 4. High-level architecture

### 4.1 Components

1. CLI adapter
   - Parses commands and prints human or JSON output.
   - Lives at `hermes_supervisor/cli.py`.

2. Config loader
   - Resolves workspace defaults, runtime dirs, command templates, and timeout policy.
   - Reads optional `~/.hermes/supervisor/config.json`.

3. State store
   - SQLite access layer.
   - Owns gateway records, event log, and reconciliation bookkeeping.

4. Gateway controller
   - Starts gateways, stops gateways, and reconciles PID/PGID facts with persisted state.

5. Activity tracker
   - Updates `last_activity_at` whenever `gateway start` is invoked for a profile.
   - Optional future hook/plugin can call the same contract before a tool/profile action.

6. Idle reaper
   - Runs on demand via CLI and optionally via cron/launchd.
   - Stops idle non-default gateways and any profile with an explicit timeout override.

7. Safety sweeper
   - Finds supervisor-owned orphan process groups and force-cleans them.
   - Cleans stale lock files and stale state rows after process verification.

8. Process-tree helper
   - Starts child gateways in a fresh process group.
   - Sends TERM to the group first, then KILL if grace period expires.
   - Verifies disappearance of the whole tree, not just the root pid.

### 4.2 Integration modes

Preferred mode:
- A thin plugin or wrapper invokes `gateway start --profile <name>` before the profile gateway is needed.
- This provides lazy-start and updates activity without changing Hermes core.

Fallback mode:
- Operator-managed wrappers or scripts call the same CLI directly.
- Reaper and sweep still work, but activity freshness is only as accurate as wrapper usage.

## 5. Runtime data model

SQLite database path:
- `~/.hermes/supervisor/state.db`

Required tables:

### 5.1 `gateways`

One row per profile.

Fields:
- `profile` TEXT PRIMARY KEY
- `is_default` INTEGER NOT NULL
- `state` TEXT NOT NULL
- `pid` INTEGER NULL
- `pgid` INTEGER NULL
- `generation` TEXT NOT NULL
- `command` TEXT NOT NULL
- `cwd` TEXT NOT NULL
- `started_at` TEXT NULL
- `last_activity_at` TEXT NULL
- `idle_timeout_seconds` INTEGER NULL
- `stop_requested_at` TEXT NULL
- `stopped_at` TEXT NULL
- `stop_reason` TEXT NULL
- `last_exit_code` INTEGER NULL
- `last_error` TEXT NULL
- `updated_at` TEXT NOT NULL

State enum:
- `stopped`
- `starting`
- `running`
- `stopping`
- `failed`

### 5.2 `events`

Append-only audit log.

Fields:
- `id` INTEGER PRIMARY KEY
- `profile` TEXT NULL
- `event_type` TEXT NOT NULL
- `at` TEXT NOT NULL
- `payload_json` TEXT NOT NULL

Required event types:
- `start_requested`
- `start_reused`
- `start_succeeded`
- `start_failed`
- `activity_touched`
- `stop_requested`
- `stop_graceful`
- `stop_forced`
- `stop_already_stopped`
- `reap_candidate`
- `reap_stopped`
- `sweep_candidate`
- `sweep_cleaned`
- `reconcile_stale_state`
- `reconcile_missing_process`

### 5.3 `locks`

Optional bookkeeping table if file locks alone are insufficient.

Fields:
- `name` TEXT PRIMARY KEY
- `owner` TEXT NOT NULL
- `acquired_at` TEXT NOT NULL

File locking remains the required mutual exclusion primitive; this table is for visibility only.

## 6. Ownership and safety rules

A process tree is supervisor-owned only if all of the following are true:
- it was started by `gateway start`
- its `pid` and `pgid` are recorded in `gateways`
- it carries environment metadata at launch time:
  - `HERMES_SUPERVISOR_PROFILE=<profile>`
  - `HERMES_SUPERVISOR_GENERATION=<generation>`
  - `HERMES_SUPERVISOR_STATE_DB=<absolute path>`

The implementation must not kill by a broad process-name pattern alone.

If state and runtime disagree:
- prefer runtime truth for whether a pid/pgid exists
- preserve an event log entry before mutating state
- never kill an ambiguous process; mark the row `failed` and require an operator-visible status result instead

## 7. Lifecycle policy

### 7.1 Lazy-start

Trigger:
- any external caller that needs a profile gateway must call `gateway start --profile <name>` first

Rules:
- if a healthy gateway already exists for the profile, return success without restarting it
- if the stored pid is missing, reconcile the stale row and start a new generation
- successful `start` updates `last_activity_at`
- start must create exactly one process group per profile generation

### 7.2 Idle-stop

Policy defaults:
- non-default profiles become reap candidates when `now - last_activity_at >= 900s`
- default profile is skipped unless either:
  - `idle_timeout_seconds` is explicitly configured for that profile, or
  - the operator passes `--include-default`

Stop sequence:
1. mark row `stopping`
2. send graceful signal to process group
3. wait `grace_seconds` for full tree exit
4. if still alive, send force signal to process group
5. verify root pid and descendants are gone
6. mark row `stopped`, set `stopped_at`, `stop_reason`

Default timeouts:
- `grace_seconds = 10`
- `force_kill_after_seconds = 5` after grace expires

### 7.3 Safety sweep

Purpose:
- remove supervisor-owned process groups that survived crashes, stale state, or partial cleanup

Rules:
- sweep interval target is 1800 seconds
- sweep only acts on supervisor-owned processes with no matching live gateway row or with a row proving the process should be stopped
- sweep may also clean stale lock files older than 2x the longest command timeout once process liveness is disproven
- sweep records every candidate and action in `events`

### 7.4 Failure handling

If start fails:
- row becomes `failed`
- `last_error` is populated
- exit code is non-zero
- no partial `running` state remains

If stop cannot verify disappearance:
- row becomes `failed`
- `status` must report `health=uncertain`
- later `sweep` may retry cleanup

## 8. CLI contract

Primary Python entrypoints:
- `python3 supervisor.py ...`
- `python3 -m hermes_supervisor ...`

Expected wrapper scripts in `scripts/`:
- `start` => `python3 supervisor.py gateway start ...`
- `stop` => `python3 supervisor.py gateway stop ...`
- `status` => `python3 supervisor.py gateway status ...`
- `reap` => `python3 supervisor.py reap ...`
- `sweep` => `python3 supervisor.py sweep ...`

### 8.1 Common CLI rules

Common flags accepted by all commands where applicable:
- `--json` return machine-readable JSON on stdout
- `--state-dir <path>` override `~/.hermes/supervisor`
- `--log-level <debug|info|warn|error>`
- `--profile <name>` for single-profile commands

Output rules:
- human mode prints one concise summary line plus detail lines if needed
- JSON mode prints exactly one JSON object
- diagnostics go to stderr; contract output goes to stdout

Exit codes:
- `0` success
- `1` usage or validation error
- `2` configuration error
- `3` requested profile not found or not registered for this command
- `4` runtime operation failed
- `5` status is degraded but command completed inspection

### 8.2 `gateway start`

Contract:
- ensure a gateway exists and is usable for the given profile
- update activity timestamp
- never create a second live generation for the same profile

Required flags:
- `--profile <name>`
- `--command <argv...>` only when the profile has no configured command template

Optional flags:
- `--cwd <path>`
- `--idle-timeout-seconds <n>` override for this profile
- `--default-profile` marks the row as default-profile policy
- `--warm-seconds <n>` optional no-op policy field for future use; implementation may persist it but does not need to act on it in v1

Success JSON shape:
```json
{
  "ok": true,
  "command": "gateway start",
  "profile": "researcher",
  "action": "started",
  "state": "running",
  "pid": 12345,
  "pgid": 12345,
  "generation": "20260703T205501Z-8f2a",
  "last_activity_at": "2026-07-03T20:55:01Z",
  "idle_timeout_seconds": 900
}
```

Allowed `action` values:
- `started`
- `reused`
- `recovered_after_stale_state`

### 8.3 `gateway stop`

Contract:
- stop a gateway if present
- safe if already stopped

Required flags:
- `--profile <name>`

Optional flags:
- `--reason <manual|idle|reap|sweep|shutdown>` default `manual`
- `--grace-seconds <n>` default `10`
- `--force-after-seconds <n>` default `5`

Success JSON shape:
```json
{
  "ok": true,
  "command": "gateway stop",
  "profile": "researcher",
  "action": "stopped",
  "state": "stopped",
  "stop_reason": "manual",
  "graceful": true,
  "pid": 12345,
  "pgid": 12345,
  "stopped_at": "2026-07-03T21:12:14Z"
}
```

Allowed `action` values:
- `stopped`
- `already_stopped`
- `forced`

### 8.4 `gateway status`

Contract:
- inspect one profile or all profiles
- reconcile DB state with runtime liveness
- never mutate running processes except for harmless state cleanup

Flags:
- `--profile <name>` or `--all`
- `--touch` optional; if provided with `--profile`, updates `last_activity_at` without starting or stopping the gateway

Success JSON shape for `--profile`:
```json
{
  "ok": true,
  "command": "gateway status",
  "profile": "researcher",
  "state": "running",
  "health": "healthy",
  "pid": 12345,
  "pgid": 12345,
  "generation": "20260703T205501Z-8f2a",
  "last_activity_at": "2026-07-03T21:03:10Z",
  "idle_for_seconds": 125,
  "idle_timeout_seconds": 900,
  "reap_due": false
}
```

Allowed `health` values:
- `healthy`
- `missing_process`
- `stale_state`
- `uncertain`
- `stopped`

`status --all` returns:
```json
{
  "ok": true,
  "command": "gateway status",
  "profiles": [ ... ],
  "summary": {
    "running": 2,
    "stopped": 5,
    "degraded": 1
  }
}
```

Exit code rules:
- `0` if all inspected profiles are healthy or cleanly stopped
- `5` if any inspected profile is degraded (`missing_process`, `stale_state`, `uncertain`)

### 8.5 `reap`

Contract:
- evaluate idle policy and stop eligible gateways
- default scope is all known profiles

Flags:
- `--all` optional explicit form; default behavior is all profiles
- `--profile <name>` optional narrow scope
- `--include-default` include default profile in policy evaluation
- `--dry-run`
- `--grace-seconds <n>`
- `--force-after-seconds <n>`

Success JSON shape:
```json
{
  "ok": true,
  "command": "reap",
  "dry_run": false,
  "evaluated": 6,
  "candidates": 2,
  "stopped": 2,
  "skipped": 4,
  "results": [
    {
      "profile": "researcher",
      "action": "stopped",
      "reason": "idle",
      "idle_for_seconds": 1120
    }
  ]
}
```

### 8.6 `sweep`

Contract:
- detect and clean supervisor-owned orphaned process trees and stale locks
- must be safe to run repeatedly

Flags:
- `--dry-run`
- `--json`
- `--max-age-seconds <n>` optional stale-lock threshold override

Success JSON shape:
```json
{
  "ok": true,
  "command": "sweep",
  "dry_run": false,
  "orphan_candidates": 1,
  "process_groups_killed": 1,
  "stale_rows_cleaned": 1,
  "stale_locks_removed": 0,
  "results": [
    {
      "profile": "researcher",
      "generation": "20260703T205501Z-8f2a",
      "action": "killed_orphan_group"
    }
  ]
}
```

## 9. Locking and concurrency

Required lock files under `~/.hermes/supervisor/locks/`:
- `global.lock`
- `<profile>.lock`

Rules:
- `gateway start` and `gateway stop` take the profile lock
- `reap` takes global lock, then profile locks one-by-one in sorted profile order
- `sweep` takes global lock
- commands must fail fast with exit code `4` if lock acquisition exceeds 30 seconds unless a future config adds waiting behavior

This prevents duplicate starts and conflicting stop/sweep races.

## 10. Directory layout

### 10.1 Code workspace

Implemented layout under `<repo-root>`:

```text
hermes-supervisor/
  docs/
    hermes-supervisor-spec.md
  hermes_supervisor/
    __init__.py
    __main__.py
    cli.py
  scripts/
    start
    stop
    status
    reap
    sweep
  tests/
    test_supervisor.py
  supervisor.py
  README.md
  INSTALL.md
  TROUBLESHOOTING.md
```

Notes:
- `supervisor.py` remains the simple console entrypoint.
- `hermes_supervisor/cli.py` contains the prototype controller, SQLite state layer, process-group helper, reaper, and sweeper. These can be split into dedicated modules later without changing the CLI contract.
- package metadata can be added later, but it is not required for the first verified implementation.

### 10.2 Runtime layout under `~/.hermes`

Implemented runtime layout:

```text
~/.hermes/
  supervisor/
    state.db
    locks/
      global.lock
      profile-<profile>.lock
    logs/
      <profile>-<generation>.log
    run/
    events/
      latest-reap.json
      latest-sweep.json
    hooks/
      pre-sweep.d/
      post-sweep.d/
      pre-stop.d/
      post-stop.d/
```

Rules:
- code must not be copied into Hermes core source directories
- runtime state must not be stored inside Hermes core source directories
- plugin files, if used in a later task, must delegate to the external workspace CLI

## 11. Minimal plugin contract

If Hermes exposes a plugin or hook mechanism, the plugin must remain thin.

Allowed plugin responsibilities:
- determine the active profile name
- call `python3 <repo-root>/supervisor.py gateway start --profile <name> --json`
- optionally call `gateway status --profile <name> --touch --json`
- surface failures to operators

Disallowed plugin responsibilities:
- embedding process management logic
- storing duplicate supervisor state
- patching Hermes core internals

## 12. Implementation sequence

Phase 1: CLI and state foundation
1. add `hermes_supervisor/cli.py`
2. add config loader and SQLite state store
3. implement `gateway status` against DB plus OS liveness checks

Phase 2: start/stop control
4. implement process-group start
5. implement graceful stop plus forced tree cleanup
6. wire `scripts/status`

Phase 3: policy commands
7. implement `reap`
8. implement `sweep`
9. add optional plugin wrapper or launchd/cron integration outside core

Phase 4: verification and docs alignment
10. add automated tests
11. replace prototype README/INSTALL/TROUBLESHOOTING claims with verified behavior
12. document rollback steps that leave Hermes core untouched

## 13. Acceptance criteria for implementation

A backend implementation is acceptable only if all criteria below are met.

### 13.1 Contract completeness
- `docs/hermes-supervisor-spec.md` exists and matches the implemented command names.
- `python3 supervisor.py gateway start|stop|status`, `python3 supervisor.py reap`, and `python3 supervisor.py sweep` all exist.
- `scripts/start`, `scripts/stop`, `scripts/status`, `scripts/reap`, and `scripts/sweep` exist and delegate to the Python entrypoint.

### 13.2 Behavior
- `gateway start` is idempotent for the same profile and never leaves two live generations for one profile.
- `gateway stop` is idempotent and verifies full process-tree shutdown.
- `gateway status --json` returns the documented fields.
- `reap` stops idle non-default profiles according to timeout policy.
- `sweep` cleans proven-orphaned supervisor-owned process groups without killing unrelated processes.
- default profile is skipped by `reap` unless explicitly configured or included.

### 13.3 Safety
- no Hermes core source files are modified
- supervisor state is stored under `~/.hermes/supervisor/`
- process cleanup uses ownership metadata plus process-group checks, not process-name matching alone
- all destructive actions are logged to the `events` table

### 13.4 Verification
- automated tests cover start reuse, stale-state recovery, graceful stop, forced stop, reap selection, and sweep cleanup
- manual smoke commands succeed on macOS in the target workspace
- docs are updated so README/INSTALL/TROUBLESHOOTING no longer claim the workspace is non-runnable once implementation exists

## 14. Verification matrix for future implementation

Required manual verification commands after implementation:

```sh
cd <repo-root>
python3 supervisor.py gateway start --profile demo --command 'sleep 600' --json
python3 supervisor.py gateway status --profile demo --json
python3 supervisor.py gateway stop --profile demo --reason manual --json
python3 supervisor.py reap --all --dry-run --json
python3 supervisor.py sweep --dry-run --json
```

Required scenario checks:

1. Lazy-start
   - start a stopped profile
   - verify `action=started`
   - call start again
   - verify `action=reused`

2. Idle-stop
   - set a short timeout for a non-default test profile
   - wait past timeout
   - run `reap`
   - verify state becomes `stopped` and the process tree is gone

3. Forced cleanup
   - simulate a child that ignores graceful shutdown
   - run `gateway stop`
   - verify forced process-group kill is recorded

4. Safety sweep
   - create a stale row or orphaned supervisor-owned process
   - run `sweep`
   - verify cleanup result and event log

5. Default-profile protection
   - mark a profile as default
   - run `reap` without `--include-default`
   - verify it is skipped

## 15. Out-of-scope follow-up tasks

These are expected follow-up implementation tasks, not part of this spec artifact:
- add actual gateway command templates per profile
- choose launchd vs cron for periodic `reap` and `sweep`
- add packaging metadata if distribution outside the workspace becomes necessary
- add richer metrics export if operators need dashboards

## 16. Final contract summary

The supervisor is an external, stdlib-only Python service boundary with:
- code in `<repo-root>`
- runtime state in `~/.hermes/supervisor/`
- SQLite as source of truth
- idempotent CLI commands for `start`, `stop`, `status`, `reap`, and `sweep`
- lazy-start by explicit wrapper/plugin invocation
- non-default idle-stop after 15 minutes by default
- conservative safety sweep every 30 minutes
- zero Hermes core source modification
