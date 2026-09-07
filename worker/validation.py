"""Test-command detection and post-change diff validation."""

from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path

from .models import DiffMeasurement, ValidationResult, ValidationStatus
from .processes import CommandRunner, CommandTimedOut, SubprocessCommandRunner
from .validation_sandbox import DockerValidationRunner, ValidationSandboxError


MAX_LOG_BYTES = 1_000_000


def _write_private(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = content.encode("utf-8", errors="replace")[-MAX_LOG_BYTES:]
    descriptor = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(encoded)


def _failure_signature(output: str, *paths: Path) -> str:
    normalized = output
    for path in paths:
        normalized = normalized.replace(str(path), "<repository>")
    return " ".join(normalized.split())[-20_000:]


def detect_test_command(repository: Path) -> tuple[str, ...] | None:
    """Choose a reproducible command without invoking a shell."""
    agents = repository / "AGENTS.md"
    if agents.is_file():
        text = agents.read_text(encoding="utf-8", errors="replace")[:100_000]
        match = re.search(
            r"(?im)^\s*(?:test(?:s| command)?|pruebas?|comando de pruebas?)\s*:\s*`([^`]+)`",
            text,
        )
        if match:
            try:
                command = tuple(shlex.split(match.group(1)))
            except ValueError:
                command = ()
            if command:
                return command

    makefile = repository / "Makefile"
    if makefile.is_file():
        content = makefile.read_text(encoding="utf-8", errors="replace")
        if re.search(r"(?m)^test\s*:", content):
            return ("make", "test")

    package_json = repository / "package.json"
    if package_json.is_file():
        try:
            package = json.loads(package_json.read_text(encoding="utf-8"))
            test_script = package.get("scripts", {}).get("test")
        except (OSError, json.JSONDecodeError, AttributeError):
            test_script = None
        if isinstance(test_script, str) and test_script.strip() and "no test specified" not in test_script:
            return ("npm", "test")

    if (repository / "go.mod").is_file():
        return ("go", "test", "./...")
    if (repository / "Cargo.toml").is_file():
        return ("cargo", "test")
    if (repository / "tests").is_dir() and (
        (repository / "pyproject.toml").is_file()
        or (repository / "pytest.ini").is_file()
        or (repository / "setup.cfg").is_file()
    ):
        if (repository / "uv.lock").is_file():
            return ("uv", "run", "--offline", "--no-sync", "--no-env-file", "python", "-m", "pytest")
        return ("python3", "-m", "pytest")
    if (repository / "tests").is_dir():
        return ("python3", "-m", "unittest", "discover", "-s", "tests")
    return None


class Validator:
    def __init__(
        self,
        *,
        git_executable: str = "git",
        runner: CommandRunner | None = None,
        timeout_seconds: float = 600.0,
        enabled: bool = False,
        test_runner: DockerValidationRunner | None = None,
    ) -> None:
        self.git = git_executable
        self.runner = runner or SubprocessCommandRunner()
        self.timeout_seconds = timeout_seconds
        self.enabled = enabled
        self.test_runner = test_runner

    def validate(
        self,
        worktree: Path,
        *,
        base_repository: Path,
        base_commit: str,
        job_directory: Path,
    ) -> ValidationResult:
        if not self.enabled:
            return ValidationResult(
                ValidationStatus.UNAVAILABLE,
                detail=(
                    "Repository test execution is disabled until it runs inside "
                    "a dedicated sandbox."
                ),
            )

        if self.test_runner is None:
            return ValidationResult(
                ValidationStatus.UNAVAILABLE,
                detail="No isolated test runner is configured; host execution is prohibited.",
            )

        log_path = job_directory / "validation.log"
        baseline_path = job_directory / "baseline"
        baseline_added = False
        try:
            add = self.runner.run(
                (
                    self.git,
                    "-c", "core.hooksPath=/dev/null",
                    "-c", "submodule.recurse=false",
                    "-C",
                    str(base_repository),
                    "worktree",
                    "add",
                    "--detach",
                    str(baseline_path),
                    base_commit,
                )
            )
            if add.returncode != 0:
                return ValidationResult(
                    ValidationStatus.UNAVAILABLE,
                    detail="Could not create a trusted base worktree for validation.",
                )
            baseline_added = True
            command = detect_test_command(baseline_path)
            if command is None:
                return ValidationResult(
                    ValidationStatus.UNAVAILABLE,
                    detail="No reproducible test command exists at the base commit.",
                )

            try:
                isolated = self.test_runner.prepare(baseline_path)
            except (OSError, CommandTimedOut) as error:
                return ValidationResult(
                    ValidationStatus.UNAVAILABLE,
                    command=command,
                    detail=(str(error) if isinstance(error, ValidationSandboxError)
                            else "Could not prepare the isolated validation environment."),
                )

            try:
                changed = isolated.run(
                    command, cwd=worktree, timeout=self.timeout_seconds
                )
            except CommandTimedOut:
                _write_private(log_path, "Validation timed out.\n")
                return ValidationResult(
                    ValidationStatus.TIMED_OUT,
                    command=command,
                    log_path=str(log_path),
                    detail="Validation exceeded its configured timeout.",
                )
            except OSError as error:
                _write_private(log_path, "Validation command is unavailable.\n")
                return ValidationResult(
                    ValidationStatus.UNAVAILABLE,
                    command=command,
                    log_path=str(log_path),
                    detail=(str(error) if isinstance(error, ValidationSandboxError)
                            else "The isolated validation command is unavailable."),
                )

            changed_output = changed.stdout + "\n" + changed.stderr
            _write_private(log_path, changed_output)
            if changed.returncode == 0:
                return ValidationResult(
                    ValidationStatus.PASSED,
                    command=command,
                    exit_code=0,
                    log_path=str(log_path),
                )

            baseline_exit: int | None = None
            baseline_output: str | None = None
            try:
                baseline = isolated.run(
                    command, cwd=baseline_path, timeout=self.timeout_seconds
                )
                baseline_exit = baseline.returncode
                baseline_output = baseline.stdout + "\n" + baseline.stderr
            except (CommandTimedOut, OSError):
                baseline_exit = None

            if baseline_output is not None:
                _write_private(
                    log_path,
                    "[changed worktree]\n"
                    + changed_output
                    + "\n[base commit]\n"
                    + baseline_output,
                )

            same_failure = bool(
                baseline_exit is not None
                and baseline_exit == changed.returncode
                and baseline_output is not None
                and _failure_signature(changed_output, worktree)
                == _failure_signature(baseline_output, baseline_path)
            )
            status = (
                ValidationStatus.UNCHANGED_FAILURE
                if same_failure
                else ValidationStatus.FAILED
            )
            return ValidationResult(
                status,
                command=command,
                exit_code=changed.returncode,
                baseline_exit_code=baseline_exit,
                log_path=str(log_path),
                detail=(
                    "The validation failure also occurs at the base commit."
                    if status is ValidationStatus.UNCHANGED_FAILURE
                    else "Validation failed after the change."
                ),
            )
        except OSError:
            return ValidationResult(
                ValidationStatus.UNAVAILABLE,
                detail="Could not create a trusted base worktree for validation.",
            )
        finally:
            if baseline_added or baseline_path.exists():
                try:
                    self.runner.run(
                        (
                            self.git,
                            "-C",
                            str(base_repository),
                            "worktree",
                            "remove",
                            "--force",
                            str(baseline_path),
                        )
                    )
                except OSError:
                    pass

    def measure_diff(
        self,
        worktree: Path,
        *,
        base_commit: str,
        max_files: int,
        max_lines: int,
    ) -> DiffMeasurement:
        result = self.runner.run(
            (self.git, "diff", "--numstat", "-M", base_commit), cwd=worktree
        )
        if result.returncode != 0:
            raise RuntimeError("git could not measure the prepared diff")

        changed_paths: set[str] = set()
        binary_paths: set[str] = set()
        changed_lines = 0
        for raw_line in result.stdout.splitlines():
            parts = raw_line.split("\t", 2)
            if len(parts) != 3:
                continue
            added, deleted, path = parts
            changed_paths.add(path)
            if added == "-" or deleted == "-":
                binary_paths.add(path)
            else:
                changed_lines += int(added) + int(deleted)

        untracked = self.runner.run(
            (self.git, "ls-files", "--others", "--exclude-standard", "-z"), cwd=worktree
        )
        for relative in filter(None, untracked.stdout.split("\x00")):
            if relative in changed_paths:
                continue
            changed_paths.add(relative)
            path = worktree / relative
            if path.is_symlink() or not path.is_file():
                binary_paths.add(relative)
                continue
            file_lines = 0
            has_content = False
            ends_with_newline = True
            with path.open("rb") as stream:
                while chunk := stream.read(64 * 1024):
                    has_content = True
                    if b"\x00" in chunk:
                        binary_paths.add(relative)
                        break
                    file_lines += chunk.count(b"\n")
                    ends_with_newline = chunk.endswith(b"\n")
            if relative not in binary_paths and has_content:
                changed_lines += file_lines + (0 if ends_with_newline else 1)

        over_budget = (
            len(changed_paths) > max_files
            or changed_lines > max_lines
            or bool(binary_paths)
        )
        reasons: list[str] = []
        if len(changed_paths) > max_files:
            reasons.append(f"{len(changed_paths)} files exceeds the limit of {max_files}")
        if changed_lines > max_lines:
            reasons.append(f"{changed_lines} lines exceeds the limit of {max_lines}")
        if binary_paths:
            reasons.append("binary content requires explicit publication approval")
        return DiffMeasurement(
            changed_files=len(changed_paths),
            changed_lines=changed_lines,
            binary_files=tuple(sorted(binary_paths)),
            over_budget=over_budget,
            detail="; ".join(reasons) or None,
        )
