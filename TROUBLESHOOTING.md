# Troubleshooting Hermes Supervisor Prototype

## Check the CLI

```sh
cd <repo-root>
python3 supervisor.py --help
python3 supervisor.py --json gateway status --all
```

If import fails, run from the workspace or set `PYTHONPATH="$(pwd)"`.

## Check state

Default state database:

```sh
python3 - <<'PY'
from pathlib import Path
import sqlite3
p = Path.home() / '.hermes' / 'supervisor' / 'state.db'
print(p, p.exists())
if p.exists():
    with sqlite3.connect(p) as conn:
        print(conn.execute("select name from sqlite_master where type='table' order by name").fetchall())
PY
```

For isolated debugging, use a temporary state directory:

```sh
TMPDIR=$(mktemp -d)
python3 supervisor.py --state-dir "$TMPDIR" --json gateway list
```

## `gateway start` fails immediately

Common causes:

- The command after `--command` exits during warm-up.
- The default `hermes -p <profile> gateway run --replace` command is not available on PATH.
- The configured `--cwd` does not exist.

Inspect the supervisor log for that generation:

```sh
ls ~/.hermes/supervisor/logs
```

## Status is degraded

`health=missing_process` means the recorded PID/PGID no longer exists. The supervisor marks this as degraded instead of pretending the gateway is healthy.

Useful commands:

```sh
python3 supervisor.py --json gateway status --profile <profile>
python3 supervisor.py --json sweep --dry-run
python3 supervisor.py --json sweep
```

## Stop does not complete gracefully

If a process ignores SIGTERM, `gateway stop` waits `--grace-seconds`, sends SIGKILL, and records `action=forced` when cleanup succeeds.

Use shorter timings for verification:

```sh
python3 supervisor.py --json gateway stop --profile demo --grace-seconds 0 --force-after-seconds 0
```

If stop cannot verify that the process group disappeared, the row becomes `failed` and `sweep` can retry cleanup.

## Reaper did not stop a profile

Check these conditions:

- `default` is skipped unless `--include-default` is passed.
- The gateway must be supervisor-owned and healthy.
- `idle_for_seconds` must be at least `idle_timeout_seconds`.

```sh
python3 supervisor.py --json gateway status --profile <profile>
python3 supervisor.py --json reap --profile <profile> --dry-run
```

## Hook failure aborts operation

Executable hooks under `~/.hermes/supervisor/hooks/pre-sweep.d` or `pre-stop.d` can abort an operation with a non-zero exit code. The CLI surfaces hook stderr in the error message.

To bypass a broken hook, move it out of the hook directory or remove execute permission.

## Hermes core remains untouched

This prototype is external. Troubleshooting should stay within:

- `<repo-root>`
- `~/.hermes/supervisor/`

Do not patch Hermes core for this prototype unless a separate task explicitly authorizes that scope.
