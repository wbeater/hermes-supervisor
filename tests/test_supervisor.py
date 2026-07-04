import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def make_fake_gateway_command(tmp: Path) -> list[str]:
    calls = tmp / "calls.jsonl"
    code = (
        f"import json, os, sys, time\n"
        f"from pathlib import Path\n"
        f"calls = Path({str(calls)!r})\n"
        f"payload = {{'argv': sys.argv[1:], 'cwd': os.getcwd(), 'generation': os.environ.get('HERMES_SUPERVISOR_GENERATION'), 'profile': os.environ.get('HERMES_SUPERVISOR_PROFILE'), 'state_db': os.environ.get('HERMES_SUPERVISOR_STATE_DB')}}\n"
        f"with calls.open('a', encoding='utf-8') as fh:\n"
        f"    fh.write(json.dumps(payload, sort_keys=True) + '\\n')\n"
        f"while True:\n"
        f"    time.sleep(0.2)\n"
    )
    return [sys.executable, "-c", code]


class SupervisorCliTests(unittest.TestCase):
    def run_cli(self, args, *, tmp: Path, json_mode: bool = True, check: bool = True):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT)
        cmd = [
            sys.executable,
            "-m",
            "hermes_supervisor",
            "--state-home",
            str(tmp / "state"),
        ]
        if json_mode:
            cmd.append("--json")
        cmd += list(args)
        result = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True)
        if check and result.returncode != 0:
            raise AssertionError(f"command failed: {cmd}\nstdout={result.stdout}\nstderr={result.stderr}")
        return result

    def read_calls(self, tmp: Path):
        path = tmp / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_start_status_stop_use_the_supplied_gateway_command(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            fake = make_fake_gateway_command(tmp)

            start = self.run_cli(
                ["gateway", "start", "--profile", "worker", "--idle-timeout-seconds", "0", "--command", *fake],
                tmp=tmp,
            )
            started = json.loads(start.stdout)
            self.assertTrue(started["ok"])
            self.assertEqual(started["profile"], "worker")
            self.assertEqual(started["state"], "running")
            self.assertEqual(started["action"], "started")

            status = self.run_cli(["gateway", "status", "--profile", "worker"], tmp=tmp)
            payload = json.loads(status.stdout)
            self.assertEqual(payload["profile"], "worker")
            self.assertEqual(payload["state"], "running")
            self.assertEqual(payload["health"], "healthy")

            calls = self.read_calls(tmp)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["argv"], [])
            self.assertEqual(calls[0]["profile"], "worker")
            self.assertTrue(calls[0]["generation"])
            self.assertTrue(calls[0]["state_db"].endswith("state.db"))

            stop = self.run_cli(["gateway", "stop", "--profile", "worker"], tmp=tmp)
            stopped = json.loads(stop.stdout)
            self.assertTrue(stopped["ok"])
            self.assertEqual(stopped["profile"], "worker")
            self.assertEqual(stopped["state"], "stopped")
            self.assertEqual(stopped["action"], "stopped")

            final_status = self.run_cli(["gateway", "status", "--profile", "worker"], tmp=tmp)
            final_payload = json.loads(final_status.stdout)
            self.assertEqual(final_payload["state"], "stopped")

    def test_stop_default_requires_explicit_flag(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            result = self.run_cli(["gateway", "stop", "--profile", "default"], tmp=tmp, json_mode=False, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Refusing to stop default profile without --include-default", result.stderr)

    def test_reap_stops_idle_non_default_gateway_immediately_when_timeout_is_zero(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            fake = make_fake_gateway_command(tmp)

            try:
                self.run_cli(
                    ["gateway", "start", "--profile", "worker", "--idle-timeout-seconds", "0", "--command", *fake],
                    tmp=tmp,
                )
                reap = self.run_cli(["reap", "--profile", "worker"], tmp=tmp)
                payload = json.loads(reap.stdout)
                self.assertTrue(payload["ok"])
                self.assertEqual(payload["stopped"], 1)
                self.assertEqual(payload["results"][0]["profile"], "worker")
                self.assertEqual(payload["results"][0]["action"], "stopped")

                status = self.run_cli(["gateway", "status", "--profile", "worker"], tmp=tmp)
                self.assertEqual(json.loads(status.stdout)["state"], "stopped")
            finally:
                self.run_cli(["gateway", "stop", "--profile", "worker"], tmp=tmp, check=False)

    def test_sweep_removes_stale_locks_without_touching_gateway_state(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            state = tmp / "state"
            stale_lock = state / "locks" / "orphan.lock"
            stale_lock.parent.mkdir(parents=True, exist_ok=True)
            stale_lock.write_text("stale", encoding="utf-8")
            old = time.time() - 120
            os.utime(stale_lock, (old, old))

            sweep = self.run_cli(["sweep", "--max-age-seconds", "0"], tmp=tmp)
            payload = json.loads(sweep.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["stale_locks_removed"], 1)
            self.assertFalse(stale_lock.exists())


if __name__ == "__main__":
    unittest.main()
