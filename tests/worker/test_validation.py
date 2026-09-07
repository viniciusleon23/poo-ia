from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from worker.models import ValidationStatus
from worker.processes import CommandResult, CommandTimedOut
from worker.validation import Validator, detect_test_command
from worker.validation_sandbox import ValidationSandboxError


class FakeIsolatedTests:
    def __init__(self) -> None:
        self.results = [CommandResult(0, "tests passed")]
        self.prepared = []
        self.calls = []

    def prepare(self, repository):
        self.prepared.append(repository)
        return self

    def run(self, argv, *, cwd, timeout):
        self.calls.append((argv, cwd, timeout))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def git(cwd: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", *arguments), cwd=cwd, text=True, capture_output=True, check=True
    ).stdout.strip()


def repository_at(root: Path) -> Path:
    repository = root / "repo"
    repository.mkdir()
    git(repository, "init", "-q")
    git(repository, "config", "user.email", "tests@example.invalid")
    git(repository, "config", "user.name", "Worker Tests")
    (repository / "tracked.txt").write_text("one\ntwo\n", encoding="utf-8")
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "base")
    return repository


class ValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = repository_at(self.root)
        self.base = git(self.repository, "rev-parse", "HEAD")
        self.isolated = FakeIsolatedTests()
        self.validator = Validator(timeout_seconds=10, enabled=True, test_runner=self.isolated)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_detects_explicit_agents_command_without_shell(self) -> None:
        (self.repository / "AGENTS.md").write_text(
            "Tests: `python3 -m unittest discover -s tests`\n", encoding="utf-8"
        )
        self.assertEqual(
            detect_test_command(self.repository),
            ("python3", "-m", "unittest", "discover", "-s", "tests"),
        )

    def test_no_test_command_is_unavailable(self) -> None:
        result = self.validator.validate(
            self.repository,
            base_repository=self.repository,
            base_commit=self.base,
            job_directory=self.root / "job",
        )
        self.assertEqual(result.status, ValidationStatus.UNAVAILABLE)

    def test_enabled_without_sandbox_never_executes_on_host(self) -> None:
        host = mock.Mock()
        result = Validator(enabled=True, runner=host).validate(
            self.repository, base_repository=self.repository, base_commit=self.base,
            job_directory=self.root / "job-no-sandbox",
        )
        self.assertEqual(result.status, ValidationStatus.UNAVAILABLE)
        self.assertIn("host execution is prohibited", result.detail)
        host.run.assert_not_called()

    def test_uv_lock_uses_the_prepared_offline_environment(self) -> None:
        (self.repository / "tests").mkdir()
        (self.repository / "pyproject.toml").write_text("[project]\nname='tests'\n")
        (self.repository / "uv.lock").write_text("version=1\n")
        self.assertEqual(detect_test_command(self.repository), (
            "uv", "run", "--offline", "--no-sync", "--no-env-file", "python", "-m", "pytest",
        ))

    def test_repository_commands_are_disabled_by_default(self) -> None:
        (self.repository / "AGENTS.md").write_text(
            "Tests: `python3 -c 'raise SystemExit(99)'`\n", encoding="utf-8"
        )
        runner = mock.Mock()

        result = Validator(runner=runner).validate(
            self.repository,
            base_repository=self.repository,
            base_commit=self.base,
            job_directory=self.root / "job-disabled",
        )

        self.assertEqual(result.status, ValidationStatus.UNAVAILABLE)
        self.assertIn("disabled", (result.detail or "").casefold())
        runner.run.assert_not_called()

    def test_modified_agents_command_is_not_used_for_baseline_comparison(self) -> None:
        (self.repository / "AGENTS.md").write_text("Tests: `false`\n", encoding="utf-8")

        result = self.validator.validate(
            self.repository,
            base_repository=self.repository,
            base_commit=self.base,
            job_directory=self.root / "job-untrusted-command",
        )

        self.assertEqual(result.status, ValidationStatus.UNAVAILABLE)
        self.assertEqual(result.command, ())

    def test_passing_make_test_is_recorded(self) -> None:
        (self.repository / "Makefile").write_text(
            "test:\n\t@python3 -c 'print(42)'\n", encoding="utf-8"
        )
        git(self.repository, "add", "Makefile")
        git(self.repository, "commit", "-qm", "add trusted test command")
        base = git(self.repository, "rev-parse", "HEAD")
        (self.repository / "tracked.txt").write_text("changed\n", encoding="utf-8")
        result = self.validator.validate(
            self.repository,
            base_repository=self.repository,
            base_commit=base,
            job_directory=self.root / "job",
        )
        self.assertEqual(result.status, ValidationStatus.PASSED)
        self.assertEqual(result.command, ("make", "test"))
        self.assertEqual(self.isolated.calls[0][0], ("make", "test"))
        self.assertNotEqual(self.isolated.prepared[0], self.repository)

    def test_failure_also_on_base_is_unchanged_failure(self) -> None:
        self.isolated.results = [CommandResult(2, "same failure"), CommandResult(2, "same failure")]
        makefile = "test:\n\t@python3 -c 'raise SystemExit(2)'\n"
        (self.repository / "Makefile").write_text(makefile, encoding="utf-8")
        git(self.repository, "add", "Makefile")
        git(self.repository, "commit", "-qm", "add failing test")
        base = git(self.repository, "rev-parse", "HEAD")
        (self.repository / "tracked.txt").write_text("changed\n", encoding="utf-8")

        result = self.validator.validate(
            self.repository,
            base_repository=self.repository,
            base_commit=base,
            job_directory=self.root / "job-fail",
        )
        self.assertEqual(result.status, ValidationStatus.UNCHANGED_FAILURE)
        self.assertNotEqual(result.exit_code, 0)
        self.assertNotEqual(result.baseline_exit_code, 0)
        self.assertEqual([call[0] for call in self.isolated.calls], [("make", "test"), ("make", "test")])
        self.assertNotEqual(self.isolated.calls[0][1], self.isolated.calls[1][1])

    def test_sandbox_unavailable_and_timeout_are_not_reported_as_test_failures(self) -> None:
        (self.repository / "AGENTS.md").write_text("Tests: `python -m pytest`\n")
        git(self.repository, "add", "AGENTS.md")
        git(self.repository, "commit", "-qm", "test command")
        base = git(self.repository, "rev-parse", "HEAD")
        for error, expected in (
            (ValidationSandboxError("Docker unavailable"), ValidationStatus.UNAVAILABLE),
            (CommandTimedOut("container timed out"), ValidationStatus.TIMED_OUT),
        ):
            with self.subTest(error=type(error).__name__):
                self.isolated.results = [error]
                result = self.validator.validate(
                    self.repository, base_repository=self.repository, base_commit=base,
                    job_directory=self.root / "job-sandbox-failure",
                )
                self.assertEqual(result.status, expected)

    def test_preparation_failure_does_not_attempt_repository_tests(self) -> None:
        (self.repository / "AGENTS.md").write_text("Tests: `python -m pytest`\n")
        git(self.repository, "add", "AGENTS.md")
        git(self.repository, "commit", "-qm", "test command")
        self.isolated.prepare = mock.Mock(side_effect=ValidationSandboxError("uv image failed"))
        result = self.validator.validate(
            self.repository, base_repository=self.repository,
            base_commit=git(self.repository, "rev-parse", "HEAD"),
            job_directory=self.root / "job-build-failure",
        )
        self.assertEqual(result.status, ValidationStatus.UNAVAILABLE)
        self.assertEqual(self.isolated.calls, [])

    def test_measure_diff_counts_tracked_untracked_and_binary(self) -> None:
        (self.repository / "tracked.txt").write_text("one\nchanged\nthree\n", encoding="utf-8")
        (self.repository / "new.txt").write_text("alpha\nbeta\n", encoding="utf-8")
        first = self.validator.measure_diff(
            self.repository,
            base_commit=self.base,
            max_files=5,
            max_lines=400,
        )
        self.assertEqual(first.changed_files, 2)
        self.assertEqual(first.changed_lines, 5)
        self.assertFalse(first.over_budget)

        (self.repository / "image.bin").write_bytes(b"binary\x00data")
        second = self.validator.measure_diff(
            self.repository,
            base_commit=self.base,
            max_files=5,
            max_lines=400,
        )
        self.assertTrue(second.over_budget)
        self.assertEqual(second.binary_files, ("image.bin",))


if __name__ == "__main__":
    unittest.main()
