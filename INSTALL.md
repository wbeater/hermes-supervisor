# Install Hermes Supervisor Prototype

No external dependencies are required. The prototype uses Python 3 stdlib and the installed `hermes` CLI when the default gateway command template is used.

## Install/run from this workspace

```sh
cd <repo-root>
chmod +x supervisor.py scripts/start scripts/stop scripts/status scripts/reap scripts/sweep
python3 supervisor.py --help
```

Optional shell PATH setup:

```sh
export PATH="$(pwd)/scripts:$PATH"
```

## Runtime state

The supervisor creates runtime state on first use:

```text
~/.hermes/supervisor/state.db
~/.hermes/supervisor/locks/
~/.hermes/supervisor/logs/
~/.hermes/supervisor/events/
~/.hermes/supervisor/hooks/
```

Use `--state-dir <path>` or `HERMES_SUPERVISOR_HOME=<path>` for isolated verification.

## Smoke verification without touching real Hermes gateways

```sh
TMPDIR=$(mktemp -d)
python3 supervisor.py --state-dir "$TMPDIR" --json gateway start --profile demo --idle-timeout-seconds 1 --command 'sleep 60'
python3 supervisor.py --state-dir "$TMPDIR" --json gateway status --profile demo
python3 supervisor.py --state-dir "$TMPDIR" --json gateway stop --profile demo --reason manual
python3 supervisor.py --state-dir "$TMPDIR" --json reap --all --dry-run
python3 supervisor.py --state-dir "$TMPDIR" --json sweep --dry-run
```

## Operating against Hermes profiles

By default, `gateway start --profile <name>` runs:

```text
hermes -p <name> gateway run --replace
```

Example:

```sh
./scripts/start --profile backend-engineer
./scripts/status --profile backend-engineer
./scripts/stop --profile backend-engineer --reason manual
```

The prototype is external: it does not copy code into Hermes core and does not patch Hermes runtime files.

## Rollback

1. Stop supervisor-owned gateways you started:
   ```sh
   python3 supervisor.py --json gateway status --all
   python3 supervisor.py --json reap --all
   ```
2. If needed, stop a specific profile:
   ```sh
   python3 supervisor.py --json gateway stop --profile <profile> --reason shutdown
   ```
3. Remove only supervisor runtime state:
   ```sh
   rm -rf ~/.hermes/supervisor
   ```

Do not remove Hermes core/profile directories as part of rolling back this prototype.
