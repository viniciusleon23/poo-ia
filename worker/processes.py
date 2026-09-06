"""Injectable, shell-free process helpers."""

from __future__ import annotations

import hashlib
import os
import selectors
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence


DEFAULT_CAPTURE_LIMIT_BYTES = 1_000_000
DEFAULT_TERMINATION_GRACE_SECONDS = 1.0
_TRUNCATION_MARKER = b"[... earlier command output truncated ...]\n"


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    stdout_sha256: str | None = None
    stderr_sha256: str | None = None


class CommandTimedOut(RuntimeError):
    """A bounded child command did not finish in time."""

    def __init__(self, message: str, *, result: CommandResult | None = None) -> None:
        super().__init__(message)
        self.result = result


class CommandRunner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: float | None = None,
        stdout_path: Path | None = None,
        stderr_path: Path | None = None,
        capture_limit_bytes: int | None = None,
    ) -> CommandResult: ...


class SubprocessCommandRunner:
    """Run one command with bounded in-memory capture and process-tree cleanup."""

    def __init__(
        self,
        *,
        capture_limit_bytes: int = DEFAULT_CAPTURE_LIMIT_BYTES,
        termination_grace_seconds: float = DEFAULT_TERMINATION_GRACE_SECONDS,
    ) -> None:
        if capture_limit_bytes <= 0:
            raise ValueError("capture_limit_bytes must be positive")
        if termination_grace_seconds <= 0:
            raise ValueError("termination_grace_seconds must be positive")
        self.capture_limit_bytes = capture_limit_bytes
        self.termination_grace_seconds = termination_grace_seconds

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: float | None = None,
        stdout_path: Path | None = None,
        stderr_path: Path | None = None,
        capture_limit_bytes: int | None = None,
    ) -> CommandResult:
        limit = (
            self.capture_limit_bytes
            if capture_limit_bytes is None
            else capture_limit_bytes
        )
        if limit <= 0:
            raise ValueError("capture_limit_bytes must be positive")

        environment = os.environ.copy()
        environment.pop("WORKER_PASSWORD", None)
        process = subprocess.Popen(
            [str(part) for part in argv],
            cwd=cwd,
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            close_fds=True,
            env=environment,
            bufsize=0,
        )
        stdout_capture = _TailCapture(limit)
        stderr_capture = _TailCapture(limit)
        timed_out = _communicate_bounded(
            process,
            input_bytes=(
                input_text.encode("utf-8") if input_text is not None else None
            ),
            timeout=timeout,
            grace_seconds=self.termination_grace_seconds,
            stdout_capture=stdout_capture,
            stderr_capture=stderr_capture,
        )
        stdout, stdout_truncated, stdout_digest = stdout_capture.result()
        stderr, stderr_truncated, stderr_digest = stderr_capture.result()
        if stdout_path is not None:
            _write_private(stdout_path, stdout)
        if stderr_path is not None:
            _write_private(stderr_path, stderr)
        result = CommandResult(
            process.returncode if process.returncode is not None else -signal.SIGKILL,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
            stdout_truncated,
            stderr_truncated,
            stdout_digest,
            stderr_digest,
        )
        if timed_out:
            raise CommandTimedOut(
                f"command timed out after {timeout} seconds", result=result
            )
        return result


class _TailCapture:
    """Hash a complete stream while retaining at most its final ``limit`` bytes."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.size = 0
        self.digest = hashlib.sha256()
        self.tail = bytearray()

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        self.digest.update(chunk)
        self.size += len(chunk)
        if len(chunk) >= self.limit:
            self.tail[:] = chunk[-self.limit :]
            return
        self.tail.extend(chunk)
        excess = len(self.tail) - self.limit
        if excess > 0:
            del self.tail[:excess]

    def result(self) -> tuple[bytes, bool, str]:
        truncated = self.size > self.limit
        captured = bytes(self.tail)
        if truncated:
            retained = max(0, self.limit - len(_TRUNCATION_MARKER))
            captured = _TRUNCATION_MARKER + captured[-retained:] if retained else b""
        return captured, truncated, self.digest.hexdigest()


def _communicate_bounded(
    process: subprocess.Popen[bytes],
    *,
    input_bytes: bytes | None,
    timeout: float | None,
    grace_seconds: float,
    stdout_capture: _TailCapture,
    stderr_capture: _TailCapture,
) -> bool:
    """Drain pipes incrementally so command output never grows a spool file."""
    assert process.stdout is not None
    assert process.stderr is not None
    selector = selectors.DefaultSelector()
    outputs = {
        process.stdout.fileno(): (process.stdout, stdout_capture),
        process.stderr.fileno(): (process.stderr, stderr_capture),
    }
    for descriptor, (stream, _capture) in outputs.items():
        os.set_blocking(descriptor, False)
        selector.register(stream, selectors.EVENT_READ, ("output", descriptor))

    input_offset = 0
    if process.stdin is not None:
        if input_bytes:
            os.set_blocking(process.stdin.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE, ("input", None))
        else:
            process.stdin.close()

    started = time.monotonic()
    deadline = None if timeout is None else started + timeout
    known_groups = {process.pid}
    timed_out = False
    root_finished_at: float | None = None

    try:
        while outputs or process.poll() is None:
            if process.poll() is None:
                known_groups.update(_descendant_process_groups(process.pid))
            elif root_finished_at is None:
                root_finished_at = time.monotonic()
                _terminate_groups(known_groups, grace_seconds)

            now = time.monotonic()
            if not timed_out and deadline is not None and now >= deadline:
                timed_out = True
                known_groups.update(_descendant_process_groups(process.pid))
                _terminate_popen_tree(process, grace_seconds, known_groups)

            if root_finished_at is not None and now - root_finished_at >= grace_seconds:
                # A descendant retaining inherited pipe descriptors must not keep
                # the harness blocked indefinitely after the command has exited.
                break

            wait = 0.05
            if deadline is not None and not timed_out:
                wait = min(wait, max(0.0, deadline - now))
            for key, mask in selector.select(wait):
                kind, descriptor = key.data
                if kind == "input":
                    assert process.stdin is not None
                    try:
                        written = os.write(
                            process.stdin.fileno(), input_bytes[input_offset:]  # type: ignore[index]
                        )
                        input_offset += written
                    except (BrokenPipeError, OSError):
                        input_offset = len(input_bytes or b"")
                    if input_offset >= len(input_bytes or b""):
                        selector.unregister(process.stdin)
                        process.stdin.close()
                    continue

                stream, capture = outputs[descriptor]
                try:
                    chunk = os.read(descriptor, 64 * 1024)
                except BlockingIOError:
                    continue
                if chunk:
                    capture.feed(chunk)
                    continue
                selector.unregister(stream)
                stream.close()
                outputs.pop(descriptor, None)
    finally:
        for key in list(selector.get_map().values()):
            try:
                selector.unregister(key.fileobj)
            except Exception:
                pass
            try:
                key.fileobj.close()
            except Exception:
                pass
        selector.close()
        if process.poll() is None:
            known_groups.update(_descendant_process_groups(process.pid))
            _terminate_popen_tree(process, grace_seconds, known_groups)
        else:
            _terminate_groups(known_groups, grace_seconds)
            process.wait()
    return timed_out


def _write_private(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)


def _group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_groups(process_groups: set[int], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(_group_exists(group) for group in process_groups):
            return True
        time.sleep(min(0.05, max(0.001, deadline - time.monotonic())))
    return not any(_group_exists(group) for group in process_groups)


def _signal_groups(process_groups: set[int], signum: int) -> None:
    for process_group in sorted(process_groups, reverse=True):
        try:
            os.killpg(process_group, signum)
        except (ProcessLookupError, PermissionError):
            continue


def _terminate_groups(process_groups: set[int], grace_seconds: float) -> None:
    groups = {group for group in process_groups if group > 0}
    if not groups:
        return
    _signal_groups(groups, signal.SIGTERM)
    if not _wait_for_groups(groups, grace_seconds):
        _signal_groups(groups, signal.SIGKILL)
        _wait_for_groups(groups, grace_seconds)


def _terminate_popen_tree(
    process: subprocess.Popen[bytes],
    grace_seconds: float,
    known_groups: set[int] | None = None,
) -> None:
    groups = set(known_groups or ())
    groups.add(process.pid)
    groups.update(_descendant_process_groups(process.pid))
    _terminate_groups(groups, grace_seconds)
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        groups.update(_descendant_process_groups(process.pid))
        _signal_groups(groups, signal.SIGKILL)
        process.wait()


class ProcessController(Protocol):
    def launch(self, argv: Sequence[str], *, cwd: Path, log_path: Path) -> int: ...

    def is_alive(self, pid: int) -> bool: ...

    def is_job_process(self, pid: int, job_id: str) -> bool: ...

    def terminate_group(self, pid: int) -> None: ...


class DetachedProcessController:
    """Launch a file-backed process group that survives worker restarts."""

    def __init__(self) -> None:
        self._children: dict[int, subprocess.Popen[bytes]] = {}

    def launch(self, argv: Sequence[str], *, cwd: Path, log_path: Path) -> int:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            log_path,
            os.O_CREAT | os.O_APPEND | os.O_WRONLY,
            0o600,
        )
        try:
            environment = os.environ.copy()
            environment.pop("WORKER_PASSWORD", None)
            process = subprocess.Popen(
                [str(part) for part in argv],
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=descriptor,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
                env=environment,
            )
            self._children[process.pid] = process
        finally:
            os.close(descriptor)
        return process.pid

    def is_alive(self, pid: int) -> bool:
        if pid <= 0:
            return False
        child = self._children.get(pid)
        if child is not None:
            if child.poll() is None:
                return True
            self._children.pop(pid, None)
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        stat_path = Path(f"/proc/{pid}/stat")
        try:
            if stat_path.read_text(encoding="ascii").split()[2] == "Z":
                try:
                    os.waitpid(pid, os.WNOHANG)
                except (ChildProcessError, OSError):
                    pass
                return False
        except (OSError, IndexError):
            pass
        return True

    def is_job_process(self, pid: int, job_id: str) -> bool:
        """Verify ownership before monitoring or signalling a persisted PID."""
        if not self.is_alive(pid):
            return False
        proc_command = Path(f"/proc/{pid}/cmdline")
        try:
            arguments = [
                value.decode("utf-8", errors="replace")
                for value in proc_command.read_bytes().split(b"\x00")
                if value
            ]
        except OSError:
            # The deployed Ubuntu host exposes /proc. On another platform, fail
            # closed so a stale manifest can never signal an unrelated process.
            return False
        return "worker.job_process" in arguments and job_id in arguments

    def terminate_group(self, pid: int) -> None:
        if pid <= 0:
            return
        groups = _descendant_process_groups(pid)
        try:
            groups.add(os.getpgid(pid))
        except ProcessLookupError:
            return
        _signal_groups(groups, signal.SIGTERM)
        if not _wait_for_groups(groups, DEFAULT_TERMINATION_GRACE_SECONDS):
            _signal_groups(groups, signal.SIGKILL)
            _wait_for_groups(groups, DEFAULT_TERMINATION_GRACE_SECONDS)
        try:
            os.waitpid(pid, os.WNOHANG)
        except (ChildProcessError, OSError):
            pass
        child = self._children.pop(pid, None)
        if child is not None:
            try:
                child.wait(timeout=DEFAULT_TERMINATION_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                pass


def _descendant_process_groups(root_pid: int) -> set[int]:
    """Snapshot Linux descendant groups before the root is terminated."""
    groups: set[int] = set()
    pending = [root_pid]
    visited: set[int] = set()
    while pending:
        parent = pending.pop()
        if parent in visited:
            continue
        visited.add(parent)
        children_path = Path(f"/proc/{parent}/task/{parent}/children")
        try:
            children = [int(value) for value in children_path.read_text().split()]
        except (OSError, ValueError):
            children = []
        for child in children:
            pending.append(child)
            try:
                groups.add(os.getpgid(child))
            except ProcessLookupError:
                continue
    return groups
