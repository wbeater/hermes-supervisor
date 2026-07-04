from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import os
import shlex
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore

STATE_VERSION = 1
DEFAULT_IDLE_SECONDS = 900
DEFAULT_GRACE_SECONDS = 10
DEFAULT_FORCE_AFTER_SECONDS = 5
DEFAULT_HOOK_TIMEOUT_SECONDS = 30
RUNNING_STATES = {"starting", "running", "stopping"}
DEGRADED_HEALTH = {"missing_process", "stale_state", "uncertain"}


@dataclasses.dataclass
class StopResult:
    action: str
    graceful: bool
    ok: bool
    health: str = "stopped"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_ts(value: str | None) -> float | None:
    if not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def default_state_dir() -> Path:
    override = os.environ.get("HERMES_SUPERVISOR_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".hermes" / "supervisor"


def generation_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def json_dumps(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


class SupervisorError(Exception):
    exit_code = 4


class UsageError(SupervisorError):
    exit_code = 1


class ConfigError(SupervisorError):
    exit_code = 2


class ProfileNotFound(SupervisorError):
    exit_code = 3


class RuntimeOperationFailed(SupervisorError):
    exit_code = 4


class DegradedStatus(SupervisorError):
    exit_code = 5


class StateStore:
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir.expanduser()
        self.db_path = self.state_dir / "state.db"
        self.legacy_json_path = self.state_dir / "state.json"
        self.locks_dir = self.state_dir / "locks"
        self.logs_dir = self.state_dir / "logs"
        self.run_dir = self.state_dir / "run"
        self.events_dir = self.state_dir / "events"

    def ensure_dirs(self) -> None:
        for path in [self.state_dir, self.locks_dir, self.logs_dir, self.run_dir, self.events_dir, self.state_dir / "hooks"]:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                os.chmod(path, 0o700)

    def connect(self) -> sqlite3.Connection:
        self.ensure_dirs()
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        self.init_schema(conn)
        return conn

    def init_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS gateways (
                profile TEXT PRIMARY KEY,
                is_default INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL,
                pid INTEGER NULL,
                pgid INTEGER NULL,
                generation TEXT NOT NULL,
                command TEXT NOT NULL,
                cwd TEXT NOT NULL,
                started_at TEXT NULL,
                last_activity_at TEXT NULL,
                idle_timeout_seconds INTEGER NULL,
                stop_requested_at TEXT NULL,
                stopped_at TEXT NULL,
                stop_reason TEXT NULL,
                last_exit_code INTEGER NULL,
                last_error TEXT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile TEXT NULL,
                event_type TEXT NOT NULL,
                at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS locks (
                name TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                acquired_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(STATE_VERSION),),
        )
        conn.commit()
        with contextlib.suppress(OSError):
            os.chmod(self.db_path, 0o600)

    @contextlib.contextmanager
    def lock(self, name: str, timeout_seconds: int = 30):
        self.ensure_dirs()
        lock_path = self.locks_dir / f"{safe_name(name)}.lock"
        deadline = time.time() + timeout_seconds
        with lock_path.open("a+") as fh:
            while True:
                try:
                    if fcntl is not None:
                        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.time() >= deadline:
                        raise RuntimeOperationFailed(f"Timed out acquiring lock {lock_path}")
                    time.sleep(0.05)
            try:
                with self.connect() as conn:
                    conn.execute(
                        "INSERT INTO locks(name, owner, acquired_at) VALUES(?,?,?) "
                        "ON CONFLICT(name) DO UPDATE SET owner=excluded.owner, acquired_at=excluded.acquired_at",
                        (name, f"pid:{os.getpid()}", utc_now()),
                    )
                yield
            finally:
                with contextlib.suppress(Exception):
                    with self.connect() as conn:
                        conn.execute("DELETE FROM locks WHERE name=?", (name,))
                if fcntl is not None:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value)


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    data["is_default"] = bool(data.get("is_default"))
    data["command"] = json_loads(data.get("command"), [])
    return data


def insert_event(conn: sqlite3.Connection, profile: str | None, event_type: str, payload: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO events(profile, event_type, at, payload_json) VALUES(?,?,?,?)",
        (profile, event_type, utc_now(), json_dumps(payload)),
    )


def load_gateway(conn: sqlite3.Connection, profile: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM gateways WHERE profile=?", (profile,)).fetchone()
    return row_to_dict(row)


def all_gateways(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM gateways ORDER BY profile").fetchall()
    return [row_to_dict(row) for row in rows if row is not None]


def pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def pgid_alive(pgid: int | None) -> bool:
    if not pgid or pgid <= 0:
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def wait_pid_exit(pid: int, timeout_seconds: float) -> bool:
    deadline = time.time() + max(0, timeout_seconds)
    while time.time() <= deadline:
        if not pid_alive(pid):
            return True
        with contextlib.suppress(ChildProcessError, OSError):
            waited, _status = os.waitpid(pid, os.WNOHANG)
            if waited == pid:
                return True
        time.sleep(0.05)
    return not pid_alive(pid)


def normalize_command(command_parts: list[str] | None, profile: str, accept_hooks: bool) -> list[str]:
    if not command_parts:
        args = ["hermes", "-p", profile, "gateway", "run", "--replace"]
        if accept_hooks:
            args.append("--accept-hooks")
        return args
    if len(command_parts) == 1:
        return shlex.split(command_parts[0])
    return command_parts


def command_line(command: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in command)


def idle_timeout_for(row: dict[str, Any]) -> int:
    value = row.get("idle_timeout_seconds")
    if value is None:
        return DEFAULT_IDLE_SECONDS
    return int(value)


def idle_for_seconds(row: dict[str, Any]) -> int:
    basis = parse_ts(row.get("last_activity_at")) or parse_ts(row.get("started_at")) or time.time()
    return max(0, int(time.time() - basis))


def runtime_health(row: dict[str, Any] | None) -> str:
    if row is None:
        return "stopped"
    state = row.get("state")
    pid = row.get("pid")
    pgid = row.get("pgid")
    if state in RUNNING_STATES:
        if pid_alive(pid) and pgid_alive(pgid):
            return "healthy"
        return "missing_process"
    if state == "failed":
        if pid_alive(pid) or pgid_alive(pgid):
            return "uncertain"
        return "stale_state"
    return "stopped"


def write_latest(store: StateStore, name: str, payload: dict[str, Any]) -> None:
    store.events_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = store.events_dir / f"latest-{name}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


def run_hooks(store: StateStore, group: str, *, profile: str | None = None, timeout: int = DEFAULT_HOOK_TIMEOUT_SECONDS) -> list[dict[str, Any]]:
    hook_dir = store.state_dir / "hooks" / f"{group}.d"
    if not hook_dir.exists():
        return []
    if not hook_dir.is_dir():
        raise RuntimeOperationFailed(f"Hook path is not a directory: {hook_dir}")
    results: list[dict[str, Any]] = []
    for hook in sorted(hook_dir.iterdir()):
        if not hook.is_file() or not (hook.stat().st_mode & stat.S_IXUSR):
            continue
        env = os.environ.copy()
        env.update({"HERMES_SUPERVISOR_HOME": str(store.state_dir), "HERMES_SUPERVISOR_HOOK": group})
        if profile:
            env["HERMES_SUPERVISOR_PROFILE"] = profile
        completed = subprocess.run([str(hook)], capture_output=True, text=True, timeout=timeout, env=env, check=False)
        item = {"hook": str(hook), "returncode": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr}
        results.append(item)
        if completed.returncode != 0:
            raise RuntimeOperationFailed(f"Hook failed: {hook}\n{completed.stderr or completed.stdout}")
    return results


def upsert_gateway(
    conn: sqlite3.Connection,
    *,
    profile: str,
    is_default: bool,
    state: str,
    pid: int | None,
    pgid: int | None,
    generation: str,
    command: list[str],
    cwd: str,
    started_at: str | None,
    last_activity_at: str | None,
    idle_timeout_seconds: int | None,
    stop_requested_at: str | None = None,
    stopped_at: str | None = None,
    stop_reason: str | None = None,
    last_exit_code: int | None = None,
    last_error: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO gateways(
            profile,is_default,state,pid,pgid,generation,command,cwd,started_at,last_activity_at,
            idle_timeout_seconds,stop_requested_at,stopped_at,stop_reason,last_exit_code,last_error,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(profile) DO UPDATE SET
            is_default=excluded.is_default,
            state=excluded.state,
            pid=excluded.pid,
            pgid=excluded.pgid,
            generation=excluded.generation,
            command=excluded.command,
            cwd=excluded.cwd,
            started_at=excluded.started_at,
            last_activity_at=excluded.last_activity_at,
            idle_timeout_seconds=excluded.idle_timeout_seconds,
            stop_requested_at=excluded.stop_requested_at,
            stopped_at=excluded.stopped_at,
            stop_reason=excluded.stop_reason,
            last_exit_code=excluded.last_exit_code,
            last_error=excluded.last_error,
            updated_at=excluded.updated_at
        """,
        (
            profile,
            1 if is_default else 0,
            state,
            pid,
            pgid,
            generation,
            json_dumps(command),
            cwd,
            started_at,
            last_activity_at,
            idle_timeout_seconds,
            stop_requested_at,
            stopped_at,
            stop_reason,
            last_exit_code,
            last_error,
            utc_now(),
        ),
    )


def start_gateway(args: argparse.Namespace, store: StateStore) -> dict[str, Any]:
    profile = require_profile(args)
    command = normalize_command(args.command, profile, args.accept_hooks)
    cwd = str(Path(args.cwd or os.getcwd()).resolve())
    is_default = bool(args.default_profile or profile == "default")
    idle_timeout = args.idle_timeout_seconds
    with store.lock(f"profile-{profile}"):
        with store.connect() as conn:
            existing = load_gateway(conn, profile)
            if existing and existing.get("state") in RUNNING_STATES and runtime_health(existing) == "healthy":
                now = utc_now()
                conn.execute("UPDATE gateways SET last_activity_at=?, updated_at=? WHERE profile=?", (now, now, profile))
                insert_event(conn, profile, "start_reused", {"pid": existing.get("pid"), "pgid": existing.get("pgid")})
                data = gateway_status_payload(load_gateway(conn, profile), "gateway start")
                data.update({"action": "reused", "ok": True})
                return data

            action = "started"
            if existing and existing.get("state") in RUNNING_STATES:
                insert_event(conn, profile, "reconcile_missing_process", {"previous_pid": existing.get("pid"), "previous_pgid": existing.get("pgid")})
                action = "recovered_after_stale_state"

            gen = generation_id()
            now = utc_now()
            upsert_gateway(
                conn,
                profile=profile,
                is_default=is_default,
                state="starting",
                pid=None,
                pgid=None,
                generation=gen,
                command=command,
                cwd=cwd,
                started_at=now,
                last_activity_at=now,
                idle_timeout_seconds=idle_timeout,
            )
            insert_event(conn, profile, "start_requested", {"generation": gen, "command": command, "cwd": cwd})
            conn.commit()

            if args.dry_run:
                pid = None
                pgid = None
            else:
                env = os.environ.copy()
                env.update(
                    {
                        "HERMES_SUPERVISOR_PROFILE": profile,
                        "HERMES_SUPERVISOR_GENERATION": gen,
                        "HERMES_SUPERVISOR_STATE_DB": str(store.db_path),
                    }
                )
                log_path = store.logs_dir / f"{safe_name(profile)}-{gen}.log"
                log = log_path.open("ab")
                try:
                    process = subprocess.Popen(
                        command,
                        cwd=cwd,
                        env=env,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        stdin=subprocess.DEVNULL,
                        start_new_session=True,
                        close_fds=True,
                    )
                except Exception as exc:
                    upsert_gateway(
                        conn,
                        profile=profile,
                        is_default=is_default,
                        state="failed",
                        pid=None,
                        pgid=None,
                        generation=gen,
                        command=command,
                        cwd=cwd,
                        started_at=now,
                        last_activity_at=now,
                        idle_timeout_seconds=idle_timeout,
                        last_error=str(exc),
                    )
                    insert_event(conn, profile, "start_failed", {"generation": gen, "error": str(exc)})
                    raise RuntimeOperationFailed(str(exc)) from exc
                finally:
                    log.close()
                time.sleep(max(0, args.warm_seconds))
                if process.poll() is not None:
                    code = process.returncode
                    upsert_gateway(
                        conn,
                        profile=profile,
                        is_default=is_default,
                        state="failed",
                        pid=process.pid,
                        pgid=None,
                        generation=gen,
                        command=command,
                        cwd=cwd,
                        started_at=now,
                        last_activity_at=now,
                        idle_timeout_seconds=idle_timeout,
                        last_exit_code=code,
                        last_error=f"process exited during warm-up with code {code}",
                    )
                    insert_event(conn, profile, "start_failed", {"generation": gen, "exit_code": code})
                    raise RuntimeOperationFailed(f"gateway command exited during warm-up with code {code}")
                pid = process.pid
                pgid = os.getpgid(process.pid)

            upsert_gateway(
                conn,
                profile=profile,
                is_default=is_default,
                state="running" if not args.dry_run else "stopped",
                pid=pid,
                pgid=pgid,
                generation=gen,
                command=command,
                cwd=cwd,
                started_at=now,
                last_activity_at=now,
                idle_timeout_seconds=idle_timeout,
            )
            insert_event(conn, profile, "start_succeeded", {"generation": gen, "pid": pid, "pgid": pgid, "dry_run": args.dry_run})
            row = load_gateway(conn, profile)
            data = gateway_status_payload(row, "gateway start")
            data.update({"action": action, "ok": True})
            return data


def stop_existing_gateway(
    conn: sqlite3.Connection,
    row: dict[str, Any] | None,
    *,
    reason: str,
    grace_seconds: int,
    force_after_seconds: int,
    dry_run: bool,
) -> StopResult:
    if row is None:
        return StopResult(action="already_stopped", graceful=True, ok=True)
    profile = row["profile"]
    pid = row.get("pid")
    pgid = row.get("pgid")
    command = row.get("command") or []
    cwd = row.get("cwd") or os.getcwd()
    gen = row.get("generation") or generation_id()
    idle_timeout = row.get("idle_timeout_seconds")
    is_default = bool(row.get("is_default"))
    if not pid_alive(pid) and not pgid_alive(pgid):
        upsert_gateway(
            conn,
            profile=profile,
            is_default=is_default,
            state="stopped",
            pid=pid,
            pgid=pgid,
            generation=gen,
            command=command,
            cwd=cwd,
            started_at=row.get("started_at"),
            last_activity_at=row.get("last_activity_at"),
            idle_timeout_seconds=idle_timeout,
            stopped_at=utc_now(),
            stop_reason=reason,
        )
        insert_event(conn, profile, "stop_already_stopped", {"reason": reason, "pid": pid, "pgid": pgid})
        return StopResult(action="already_stopped", graceful=True, ok=True)

    if dry_run:
        insert_event(conn, profile, "stop_requested", {"reason": reason, "pid": pid, "pgid": pgid, "dry_run": True})
        return StopResult(action="would_stop", graceful=True, ok=True)

    upsert_gateway(
        conn,
        profile=profile,
        is_default=is_default,
        state="stopping",
        pid=pid,
        pgid=pgid,
        generation=gen,
        command=command,
        cwd=cwd,
        started_at=row.get("started_at"),
        last_activity_at=row.get("last_activity_at"),
        idle_timeout_seconds=idle_timeout,
        stop_requested_at=utc_now(),
        stop_reason=reason,
    )
    insert_event(conn, profile, "stop_requested", {"reason": reason, "pid": pid, "pgid": pgid, "dry_run": False})

    graceful = True
    if pgid_alive(pgid):
        os.killpg(int(pgid), signal.SIGTERM)
    elif pid_alive(pid):
        os.kill(int(pid), signal.SIGTERM)
    exited = wait_pid_exit(int(pid), grace_seconds) if pid else True
    if not exited:
        graceful = False
        time.sleep(max(0, force_after_seconds))
        if pgid_alive(pgid):
            os.killpg(int(pgid), signal.SIGKILL)
        elif pid_alive(pid):
            os.kill(int(pid), signal.SIGKILL)
        exited = wait_pid_exit(int(pid), max(1, force_after_seconds)) if pid else True

    if exited and not pgid_alive(pgid):
        upsert_gateway(
            conn,
            profile=profile,
            is_default=is_default,
            state="stopped",
            pid=pid,
            pgid=pgid,
            generation=gen,
            command=command,
            cwd=cwd,
            started_at=row.get("started_at"),
            last_activity_at=row.get("last_activity_at"),
            idle_timeout_seconds=idle_timeout,
            stopped_at=utc_now(),
            stop_reason=reason,
        )
        insert_event(conn, profile, "stop_graceful" if graceful else "stop_forced", {"reason": reason, "pid": pid, "pgid": pgid})
        return StopResult(action="stopped" if graceful else "forced", graceful=graceful, ok=True)

    upsert_gateway(
        conn,
        profile=profile,
        is_default=is_default,
        state="failed",
        pid=pid,
        pgid=pgid,
        generation=gen,
        command=command,
        cwd=cwd,
        started_at=row.get("started_at"),
        last_activity_at=row.get("last_activity_at"),
        idle_timeout_seconds=idle_timeout,
        stop_reason=reason,
        last_error="process group did not exit after stop sequence",
    )
    insert_event(conn, profile, "stop_failed", {"reason": reason, "pid": pid, "pgid": pgid})
    return StopResult(action="failed", graceful=False, ok=False, health="uncertain")


def stop_gateway(args: argparse.Namespace, store: StateStore) -> dict[str, Any]:
    profile = require_profile(args)
    if profile == "default" and not args.include_default:
        raise UsageError("Refusing to stop default profile without --include-default")
    with store.lock(f"profile-{profile}"):
        run_hooks(store, "pre-stop", profile=profile, timeout=args.hook_timeout)
        with store.connect() as conn:
            row = load_gateway(conn, profile)
            result = stop_existing_gateway(
                conn,
                row,
                reason=args.reason,
                grace_seconds=args.grace_seconds,
                force_after_seconds=args.force_after_seconds,
                dry_run=args.dry_run,
            )
            row = load_gateway(conn, profile)
            payload = gateway_status_payload(row, "gateway stop", profile=profile)
            payload.update(
                {
                    "ok": result.ok,
                    "action": result.action,
                    "stop_reason": args.reason,
                    "graceful": result.graceful,
                    "health": result.health,
                }
            )
        run_hooks(store, "post-stop", profile=profile, timeout=args.hook_timeout)
        return payload


def gateway_status_payload(row: dict[str, Any] | None, command: str, *, profile: str | None = None) -> dict[str, Any]:
    profile_name = profile or (row or {}).get("profile")
    health = runtime_health(row)
    state = (row or {}).get("state") or "stopped"
    payload = {
        "ok": health not in DEGRADED_HEALTH,
        "command": command,
        "profile": profile_name,
        "state": state,
        "health": health,
        "pid": (row or {}).get("pid"),
        "pgid": (row or {}).get("pgid"),
        "generation": (row or {}).get("generation"),
        "last_activity_at": (row or {}).get("last_activity_at"),
        "idle_for_seconds": idle_for_seconds(row) if row else 0,
        "idle_timeout_seconds": idle_timeout_for(row) if row else DEFAULT_IDLE_SECONDS,
        "reap_due": bool(row and not row.get("is_default") and health == "healthy" and idle_for_seconds(row) >= idle_timeout_for(row)),
    }
    return payload


def status_gateway(args: argparse.Namespace, store: StateStore) -> dict[str, Any]:
    profiles = []
    if args.profile:
        profiles = [args.profile]
    with store.lock("global"):
        with store.connect() as conn:
            if profiles:
                rows = [load_gateway(conn, profile) for profile in profiles]
            else:
                rows = all_gateways(conn)
            results = []
            degraded = False
            for index, row in enumerate(rows):
                profile = profiles[index] if profiles else row["profile"]
                health = runtime_health(row)
                if row and row.get("state") in RUNNING_STATES and health == "missing_process":
                    conn.execute(
                        "UPDATE gateways SET state='failed', last_error=?, updated_at=? WHERE profile=?",
                        ("recorded process is missing", utc_now(), profile),
                    )
                    insert_event(conn, profile, "reconcile_missing_process", {"pid": row.get("pid"), "pgid": row.get("pgid")})
                    row = load_gateway(conn, profile)
                    health = runtime_health(row)
                if args.touch and args.profile:
                    now = utc_now()
                    if row is None:
                        upsert_gateway(
                            conn,
                            profile=profile,
                            is_default=profile == "default",
                            state="stopped",
                            pid=None,
                            pgid=None,
                            generation=generation_id(),
                            command=[],
                            cwd=os.getcwd(),
                            started_at=None,
                            last_activity_at=now,
                            idle_timeout_seconds=None,
                        )
                    else:
                        conn.execute("UPDATE gateways SET last_activity_at=?, updated_at=? WHERE profile=?", (now, now, profile))
                    insert_event(conn, profile, "activity_touched", {"via": "status"})
                    row = load_gateway(conn, profile)
                payload = gateway_status_payload(row, "gateway status", profile=profile)
                degraded = degraded or payload["health"] in DEGRADED_HEALTH
                results.append(payload)
            summary = {
                "running": sum(1 for item in results if item["state"] == "running"),
                "stopped": sum(1 for item in results if item["state"] == "stopped"),
                "degraded": sum(1 for item in results if item["health"] in DEGRADED_HEALTH),
            }
            if args.profile:
                return results[0] if results else gateway_status_payload(None, "gateway status", profile=args.profile)
            return {"ok": not degraded, "command": "gateway status", "profiles": results, "summary": summary}


def list_gateways(args: argparse.Namespace, store: StateStore) -> dict[str, Any]:
    del args
    with store.lock("global"):
        with store.connect() as conn:
            rows = all_gateways(conn)
            profiles = [gateway_status_payload(row, "gateway list") for row in rows]
    return {"ok": True, "command": "gateway list", "profiles": profiles, "count": len(profiles)}


def reap(args: argparse.Namespace, store: StateStore) -> dict[str, Any]:
    selected = set(args.profile or [])
    results: list[dict[str, Any]] = []
    candidates = 0
    stopped = 0
    skipped = 0
    with store.lock("global"):
        with store.connect() as conn:
            rows = all_gateways(conn)
            for row in rows:
                profile = row["profile"]
                if selected and profile not in selected:
                    continue
                health = runtime_health(row)
                idle_for = idle_for_seconds(row)
                timeout = idle_timeout_for(row)
                if row.get("is_default") and not args.include_default:
                    skipped += 1
                    results.append({"profile": profile, "action": "skipped", "reason": "default", "idle_for_seconds": idle_for})
                    continue
                if health != "healthy" or row.get("state") != "running":
                    skipped += 1
                    results.append({"profile": profile, "action": "skipped", "reason": health, "idle_for_seconds": idle_for})
                    continue
                if idle_for < timeout:
                    skipped += 1
                    results.append({"profile": profile, "action": "skipped", "reason": "below_idle_timeout", "idle_for_seconds": idle_for})
                    continue
                candidates += 1
                if args.dry_run:
                    results.append({"profile": profile, "action": "would_stop", "reason": "idle", "idle_for_seconds": idle_for})
                    continue
                run_hooks(store, "pre-stop", profile=profile, timeout=args.hook_timeout)
                stop_result = stop_existing_gateway(
                    conn,
                    row,
                    reason="idle",
                    grace_seconds=args.grace_seconds,
                    force_after_seconds=args.force_after_seconds,
                    dry_run=False,
                )
                run_hooks(store, "post-stop", profile=profile, timeout=args.hook_timeout)
                stopped += 1 if stop_result.ok else 0
                results.append({"profile": profile, "action": stop_result.action, "reason": "idle", "idle_for_seconds": idle_for})
            insert_event(conn, None, "reap_stopped", {"dry_run": args.dry_run, "candidates": candidates, "stopped": stopped})
            payload = {"ok": True, "command": "reap", "dry_run": args.dry_run, "evaluated": len(results), "candidates": candidates, "stopped": stopped, "skipped": skipped, "results": results}
            write_latest(store, "reap", payload)
            return payload


def sweep(args: argparse.Namespace, store: StateStore) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    killed = 0
    stale_rows = 0
    stale_locks = 0
    now = time.time()
    with store.lock("global"):
        run_hooks(store, "pre-sweep", timeout=args.hook_timeout)
        with store.connect() as conn:
            rows = all_gateways(conn)
            for row in rows:
                profile = row["profile"]
                health = runtime_health(row)
                if row.get("state") in {"failed", "stopping"} and pgid_alive(row.get("pgid")):
                    results.append({"profile": profile, "generation": row.get("generation"), "action": "would_kill_group" if args.dry_run else "killed_orphan_group"})
                    if not args.dry_run:
                        os.killpg(int(row["pgid"]), signal.SIGKILL)
                        wait_pid_exit(int(row["pid"]), 2) if row.get("pid") else None
                        killed += 1
                        conn.execute("UPDATE gateways SET state='stopped', stopped_at=?, stop_reason='sweep', updated_at=? WHERE profile=?", (utc_now(), utc_now(), profile))
                    continue
                if health == "missing_process":
                    stale_rows += 1
                    results.append({"profile": profile, "generation": row.get("generation"), "action": "cleaned_stale_row"})
                    if not args.dry_run:
                        conn.execute("UPDATE gateways SET state='failed', last_error=?, updated_at=? WHERE profile=?", ("missing process found by sweep", utc_now(), profile))
                        insert_event(conn, profile, "sweep_candidate", {"reason": "missing_process"})
            for lock_path in store.locks_dir.glob("*.lock"):
                try:
                    age = now - lock_path.stat().st_mtime
                except FileNotFoundError:
                    continue
                if age >= args.max_age_seconds:
                    stale_locks += 1
                    results.append({"lock": str(lock_path), "action": "would_remove_lock" if args.dry_run else "removed_lock", "age_seconds": int(age)})
                    if not args.dry_run:
                        with contextlib.suppress(FileNotFoundError):
                            lock_path.unlink()
            insert_event(conn, None, "sweep_cleaned", {"dry_run": args.dry_run, "process_groups_killed": killed, "stale_rows_cleaned": stale_rows, "stale_locks_removed": stale_locks})
            payload = {"ok": True, "command": "sweep", "dry_run": args.dry_run, "orphan_candidates": killed + stale_rows, "process_groups_killed": killed, "stale_rows_cleaned": stale_rows, "stale_locks_removed": stale_locks, "results": results}
            write_latest(store, "sweep", payload)
        run_hooks(store, "post-sweep", timeout=args.hook_timeout)
        return payload


def require_profile(args: argparse.Namespace) -> str:
    if not getattr(args, "profile", None):
        raise UsageError("--profile is required")
    return args.profile


def output_payload(payload: dict[str, Any], *, json_mode: bool) -> int:
    if json_mode:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        command = payload.get("command", "supervisor")
        ok = payload.get("ok", False)
        print(f"{command}: {'ok' if ok else 'degraded'}")
        if "profile" in payload:
            print(f"profile={payload.get('profile')} state={payload.get('state')} health={payload.get('health')} pid={payload.get('pid')} pgid={payload.get('pgid')}")
        if "action" in payload:
            print(f"action={payload.get('action')}")
        if "summary" in payload:
            print(f"summary={payload['summary']}")
        if "results" in payload:
            for item in payload["results"]:
                print(json.dumps(item, sort_keys=True))
    if payload.get("ok", False):
        return 0
    if payload.get("health") in DEGRADED_HEALTH or payload.get("summary", {}).get("degraded"):
        return 5
    return 4


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="External stdlib-only supervisor for Hermes gateways")
    parser.add_argument("--state-dir", "--state-home", dest="state_dir", default=str(default_state_dir()), help="runtime state directory (default: ~/.hermes/supervisor)")
    parser.add_argument("--json", action="store_true", help="print exactly one JSON object")
    parser.add_argument("--dry-run", action="store_true", help="validate and report without process side effects")
    parser.add_argument("--hook-timeout", type=int, default=DEFAULT_HOOK_TIMEOUT_SECONDS)
    parser.add_argument("--accept-hooks", action="store_true", help="pass through to default Hermes gateway command")

    def add_json_flag(p: argparse.ArgumentParser) -> None:
        p.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    def add_dry_run_flag(p: argparse.ArgumentParser) -> None:
        p.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS, help="validate and report without process side effects")

    sub = parser.add_subparsers(dest="command", required=True)
    gateway = sub.add_parser("gateway", help="gateway lifecycle commands")
    gateway_sub = gateway.add_subparsers(dest="gateway_command", required=True)

    p = gateway_sub.add_parser("start", help="idempotently start one profile gateway")
    add_json_flag(p)
    add_dry_run_flag(p)
    p.add_argument("--profile", required=True)
    p.add_argument("--command", nargs=argparse.REMAINDER, help="command argv to supervise; default: hermes -p PROFILE gateway run --replace")
    p.add_argument("--cwd")
    p.add_argument("--idle-timeout-seconds", type=int)
    p.add_argument("--default-profile", action="store_true")
    p.add_argument("--warm-seconds", type=float, default=0.1)
    p.set_defaults(func=start_gateway)

    p = gateway_sub.add_parser("stop", help="stop one profile gateway")
    add_json_flag(p)
    add_dry_run_flag(p)
    p.add_argument("--profile", required=True)
    p.add_argument("--reason", choices=["manual", "idle", "reap", "sweep", "shutdown"], default="manual")
    p.add_argument("--grace-seconds", type=int, default=DEFAULT_GRACE_SECONDS)
    p.add_argument("--force-after-seconds", type=int, default=DEFAULT_FORCE_AFTER_SECONDS)
    p.add_argument("--include-default", action="store_true")
    p.set_defaults(func=stop_gateway)

    p = gateway_sub.add_parser("status", help="inspect one profile or all supervisor-managed profiles")
    add_json_flag(p)
    p.add_argument("--profile")
    p.add_argument("--all", action="store_true")
    p.add_argument("--touch", action="store_true")
    p.set_defaults(func=status_gateway)

    p = gateway_sub.add_parser("list", help="list supervisor-managed profile gateways")
    add_json_flag(p)
    p.set_defaults(func=list_gateways)

    p = sub.add_parser("reap", help="stop idle non-default supervisor-owned gateways")
    add_json_flag(p)
    add_dry_run_flag(p)
    p.add_argument("--profile", action="append")
    p.add_argument("--all", action="store_true")
    p.add_argument("--include-default", action="store_true")
    p.add_argument("--grace-seconds", type=int, default=DEFAULT_GRACE_SECONDS)
    p.add_argument("--force-after-seconds", type=int, default=DEFAULT_FORCE_AFTER_SECONDS)
    p.set_defaults(func=reap)

    p = sub.add_parser("sweep", help="clean stale supervisor-owned process groups and locks")
    add_json_flag(p)
    add_dry_run_flag(p)
    p.add_argument("--max-age-seconds", type=int, default=3600)
    p.set_defaults(func=sweep)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    store = StateStore(Path(args.state_dir))
    try:
        payload = args.func(args, store)
        return output_payload(payload, json_mode=args.json)
    except SupervisorError as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc), "type": exc.__class__.__name__}, indent=2, sort_keys=True))
        else:
            print(str(exc), file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
