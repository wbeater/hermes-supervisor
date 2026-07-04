# Hermes Supervisor

Repository: wbeater/hermes-supervisor

Hermes Supervisor is an **external, stdlib-only** supervisor for Hermes gateway processes. It keeps runtime state outside Hermes core, uses SQLite as its source of truth, and manages gateway lifecycle with process-group safety.

The project is designed to stay narrow and boring:

- **No Hermes core modifications**
- **No third-party Python dependencies**
- **No broad process-name killing**
- **All runtime state under `~/.hermes/supervisor/` by default**

## What it does

| Capability | Description |
|---|---|
| Lazy start | Start a profile gateway only when needed |
| Idempotent start | Re-running `gateway start` reuses a healthy gateway instead of spawning duplicates |
| Safe stop | Stop by recorded process group, then verify the whole tree is gone |
| Status reconciliation | Detect stale rows, missing processes, and degraded state |
| Idle reaping | Stop non-default profiles after inactivity |
| Safety sweep | Clean orphaned supervisor-owned process groups and stale locks |
| Thin wrapper integration | Use scripts or plugin hooks to call the supervisor externally |

## Entry points

You can run the supervisor in either of these ways:

```sh
python3 supervisor.py ...
python3 -m hermes_supervisor ...
```

Wrapper scripts are provided in `scripts/`:

- `scripts/start` → `gateway start`
- `scripts/stop` → `gateway stop`
- `scripts/status` → `gateway status`
- `scripts/reap` → `reap`
- `scripts/sweep` → `sweep`

## Quick start

```sh
cd <repo-root>
python3 supervisor.py --help
```

If you want the helper scripts on your `PATH`:

```sh
export PATH="$(pwd)/scripts:$PATH"
```

## Commands

### `gateway start`

Start a gateway for one profile.

Default command template:

```text
hermes -p <profile> gateway run --replace
```

Example:

```sh
./scripts/start --profile backend-engineer
```

You can also supervise an explicit command, which is useful for tests and non-Hermes processes:

```sh
python3 supervisor.py gateway start \
  --profile demo \
  --command 'sleep 600' \
  --json
```

Useful flags:

- `--profile <name>`: required
- `--command ...`: override the command to supervise
- `--cwd <path>`: working directory for the gateway
- `--idle-timeout-seconds <n>`: override the idle timeout for this profile
- `--default-profile`: mark the profile as default-policy protected
- `--warm-seconds <n>`: reserved policy field

### `gateway stop`

Stop one profile gateway safely.

```sh
./scripts/stop --profile backend-engineer --reason manual
```

Useful flags:

- `--profile <name>`: required
- `--reason manual|idle|reap|sweep|shutdown`
- `--grace-seconds <n>`: how long to wait before escalation
- `--force-after-seconds <n>`: force-kill grace window
- `--include-default`: allow stopping the default profile

### `gateway status`

Inspect one profile or all supervisor-managed profiles.

```sh
./scripts/status --profile backend-engineer
python3 supervisor.py gateway status --all --json
python3 supervisor.py gateway status --profile backend-engineer --touch
```

Flags:

- `--profile <name>`: inspect a single profile
- `--all`: inspect all profiles
- `--touch`: update activity timestamp without starting/stopping

### `gateway list`

List supervisor-managed profiles.

```sh
python3 supervisor.py gateway list
```

### `reap`

Stop idle non-default gateways.

```sh
python3 supervisor.py reap --all --dry-run
python3 supervisor.py reap --profile backend-engineer
```

Useful flags:

- `--profile <name>`: narrow to one profile
- `--all`: evaluate all profiles
- `--include-default`: include the default profile in policy evaluation
- `--grace-seconds <n>`
- `--force-after-seconds <n>`

### `sweep`

Clean stale supervisor-owned process groups and locks.

```sh
python3 supervisor.py sweep --dry-run
python3 supervisor.py sweep --max-age-seconds 3600
```

## Runtime layout

By default, runtime state lives here:

```text
~/.hermes/supervisor/
  state.db
  locks/
  logs/
  run/
  events/
  hooks/
```

You can override it per run:

```sh
python3 supervisor.py --state-dir /tmp/hermes-supervisor gateway status --all --json
```

or with:

```sh
export HERMES_SUPERVISOR_HOME=/tmp/hermes-supervisor
```

## Process model

The supervisor uses a conservative ownership model:

- each started gateway runs in its own process group
- stop operations target the recorded process group, not a name glob
- sweep only cleans processes that are proven supervisor-owned
- stale database rows are reconciled against runtime truth

That means the supervisor is safe to run repeatedly and safe to recover after crashes.

## Typical workflow

1. Start a profile gateway when you need it.
2. Use `gateway status` to inspect health or touch activity.
3. Let `reap` stop idle non-default gateways.
4. Run `sweep` periodically to clean orphaned state.

Example session:

```sh
python3 supervisor.py gateway start --profile demo --command 'sleep 600' --json
python3 supervisor.py gateway status --profile demo --json
python3 supervisor.py reap --profile demo --dry-run --json
python3 supervisor.py gateway stop --profile demo --reason manual --json
python3 supervisor.py sweep --dry-run --json
```

## Installation

No external dependencies are required.

```sh
cd <repo-root>
chmod +x supervisor.py scripts/start scripts/stop scripts/status scripts/reap scripts/sweep
python3 supervisor.py --help
```

## Verification

Recommended smoke checks:

```sh
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 -m compileall -q hermes_supervisor supervisor.py tests
```

For isolated runtime verification, use a temporary state directory and a harmless command such as `sleep`.

## Troubleshooting

- **`gateway start` fails immediately**: the target command may exit during warm-up, or the default `hermes` command may not be on `PATH`.
- **Status is degraded**: the recorded PID/PGID no longer exists, so the row is stale and should be reconciled or swept.
- **Stop does not finish gracefully**: the supervisor escalates to SIGKILL after the configured grace window.
- **Hooks fail**: non-zero hook exit codes abort the operation.

See `INSTALL.md` and `TROUBLESHOOTING.md` for more detail.

## Project docs

- `docs/hermes-supervisor-spec.md` — external contract and implementation spec
- `INSTALL.md` — installation and smoke checks
- `TROUBLESHOOTING.md` — debugging guide

## Design note

This repository is intentionally external to Hermes core. If you only need gateway orchestration, this repo gives you a small, testable boundary instead of another core subsystem.
