from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from worker.processes import (
    CommandTimedOut,
    DetachedProcessController,
    SubprocessCommandRunner,
)


def process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class SubprocessCommandRunnerTests(unittest.TestCase):
    def test_explicit_environment_does_not_inherit_host_values(self) -> None:
        runner = SubprocessCommandRunner(environment={"ALLOWED_CANARY": "present", "WORKER_PASSWORD": "also-removed"})
        with mock.patch.dict(os.environ, {"DENIED_CANARY": "must-not-leak", "AWS_SECRET_ACCESS_KEY": "synthetic-key"}):
            result = runner.run((
                sys.executable, "-c",
                "import os; print(os.getenv('ALLOWED_CANARY')); print(os.getenv('DENIED_CANARY')); print(os.getenv('AWS_SECRET_ACCESS_KEY')); print(os.getenv('WORKER_PASSWORD'))",
            ), timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.splitlines(), ["present", "None", "None", "None"])

    def test_external_commands_do_not_inherit_worker_password(self) -> None:
        runner = SubprocessCommandRunner()
        with mock.patch.dict(os.environ, {"WORKER_PASSWORD": "must-not-leak"}):
            result = runner.run(
                (
                    sys.executable,
                    "-c",
                    "import os; print(os.environ.get('WORKER_PASSWORD', 'missing'))",
                ),
                timeout=5,
            )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "missing")

    def test_capture_is_bounded_and_reports_full_stream_hash(self) -> None:
        runner = SubprocessCommandRunner(capture_limit_bytes=1024)
        with mock.patch(
            "tempfile.TemporaryFile",
            side_effect=AssertionError("capture must not spool unbounded output to disk"),
        ):
            result = runner.run(
                (
                    sys.executable,
                    "-c",
                    "import sys; sys.stdout.write('x' * 200000 + 'END-MARKER')",
                ),
                timeout=5,
            )

        self.assertTrue(result.stdout_truncated)
        self.assertLessEqual(len(result.stdout.encode("utf-8")), 1024)
        self.assertTrue(result.stdout.endswith("END-MARKER"))
        self.assertRegex(result.stdout_sha256 or "", r"^[0-9a-f]{64}$")

    @unittest.skipUnless(os.name == "posix", "process groups require POSIX")
    def test_timeout_escalates_and_terminates_descendant_group(self) -> None:
        runner = SubprocessCommandRunner(
            capture_limit_bytes=4096,
            termination_grace_seconds=0.1,
        )
        child_pid: int | None = None
        script = (
            "import signal,subprocess,sys,time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "child=subprocess.Popen([sys.executable,'-c',"
            "'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)']); "
            "print(child.pid, flush=True); print('partial-marker', flush=True); time.sleep(60)"
        )

        started = time.monotonic()
        try:
            with self.assertRaises(CommandTimedOut) as raised:
                runner.run((sys.executable, "-c", script), timeout=0.2)
            self.assertLess(time.monotonic() - started, 3)
            result = raised.exception.result
            self.assertIsNotNone(result)
            assert result is not None
            lines = result.stdout.splitlines()
            child_pid = int(next(line for line in lines if line.isdecimal()))
            self.assertIn("partial-marker", result.stdout)

            deadline = time.monotonic() + 2
            while process_exists(child_pid) and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertFalse(process_exists(child_pid))
        finally:
            if child_pid and process_exists(child_pid):
                try:
                    os.kill(child_pid, 9)
                except ProcessLookupError:
                    pass

    @unittest.skipUnless(
        os.name == "posix" and Path("/proc").is_dir(),
        "escaped process-group discovery requires Linux /proc",
    )
    def test_timeout_terminates_descendant_that_created_a_new_session(self) -> None:
        runner = SubprocessCommandRunner(
            capture_limit_bytes=4096,
            termination_grace_seconds=0.1,
        )
        child_pid: int | None = None
        script = (
            "import subprocess,sys,time; "
            "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'], "
            "start_new_session=True); print(child.pid, flush=True); time.sleep(60)"
        )

        try:
            with self.assertRaises(CommandTimedOut) as raised:
                runner.run((sys.executable, "-c", script), timeout=0.2)
            assert raised.exception.result is not None
            child_pid = int(raised.exception.result.stdout.strip())
            deadline = time.monotonic() + 2
            while process_exists(child_pid) and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertFalse(process_exists(child_pid))
        finally:
            if child_pid and process_exists(child_pid):
                try:
                    os.killpg(child_pid, 9)
                except ProcessLookupError:
                    pass

    def test_timeout_persists_bounded_partial_output(self) -> None:
        runner = SubprocessCommandRunner(
            capture_limit_bytes=1024,
            termination_grace_seconds=0.1,
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "partial.log"
            with self.assertRaises(CommandTimedOut) as raised:
                runner.run(
                    (
                        sys.executable,
                        "-c",
                        "import sys,time; print('before-timeout', flush=True); time.sleep(60)",
                    ),
                    timeout=0.2,
                    stdout_path=output,
                )

            self.assertIn("before-timeout", output.read_text(encoding="utf-8"))
            self.assertIn("before-timeout", raised.exception.result.stdout)

    def test_detached_process_does_not_inherit_worker_password(self) -> None:
        controller = DetachedProcessController()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log_path = root / "detached.log"
            with mock.patch.dict(os.environ, {"WORKER_PASSWORD": "must-not-leak"}):
                pid = controller.launch(
                    (
                        sys.executable,
                        "-c",
                        "import os; print(os.environ.get('WORKER_PASSWORD', 'missing'), flush=True)",
                    ),
                    cwd=root,
                    log_path=log_path,
                )

            deadline = time.monotonic() + 3
            while (
                not log_path.exists() or not log_path.read_text(encoding="utf-8")
            ) and time.monotonic() < deadline:
                time.sleep(0.02)
            while controller.is_alive(pid) and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(log_path.read_text(encoding="utf-8").strip(), "missing")


if __name__ == "__main__":
    unittest.main()
