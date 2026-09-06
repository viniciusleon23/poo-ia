"""Crash-safe retention for terminal worker jobs.

The terminal manifest is deliberately removed last.  It therefore acts as the
recovery journal if the worker stops after removing a worktree or its private
artifacts but before the whole cleanup has completed.
"""

from __future__ import annotations

import fcntl
import os
import re
import shutil
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .models import JOB_ID_PATTERN, JobManifest, JobState, TERMINAL_STATES
from .processes import CommandResult, CommandRunner, SubprocessCommandRunner
from .store import ManifestNotFoundError, ManifestStore


DEFAULT_RETENTION_DAYS = 30
DEFAULT_RETENTION_BATCH_SIZE = 10
DEFAULT_GIT_CLEANUP_TIMEOUT_SECONDS = 60.0


class RetentionSafetyError(RuntimeError):
    """Cleanup could not prove that a destructive operation was safe."""


class JobLeaseBusy(RuntimeError):
    """A worker process still owns the requested job."""


@dataclass(frozen=True, slots=True)
class RetentionIssue:
    job_id: str
    phase: str
    detail: str


@dataclass(frozen=True, slots=True)
class RetentionReport:
    scanned: int
    eligible: int
    deleted: tuple[str, ...]
    deferred: tuple[RetentionIssue, ...]
    has_more: bool


def _safe_detail(error: BaseException) -> str:
    detail = " ".join(str(error).split())[:500]
    return detail or error.__class__.__name__


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _overlaps(first: Path, second: Path) -> bool:
    return (
        first == second
        or first.is_relative_to(second)
        or second.is_relative_to(first)
    )


def validate_retention_roots(
    *, workspace: Path, worktrees_root: Path, jobs_root: Path
) -> tuple[Path, Path, Path]:
    """Resolve and reject roots whose trees could overlap during deletion."""
    resolved = tuple(_resolved(path) for path in (workspace, worktrees_root, jobs_root))
    labels = ("workspace", "worktrees_root", "jobs_root")
    for index, first in enumerate(resolved):
        for other_index in range(index + 1, len(resolved)):
            second = resolved[other_index]
            if _overlaps(first, second):
                raise RetentionSafetyError(
                    f"{labels[index]} and {labels[other_index]} must be disjoint"
                )
    return resolved


def _private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise RetentionSafetyError(f"private directory is unsafe: {path.name}")
    try:
        path.chmod(0o700)
    except OSError:
        pass


def _open_lock(path: Path) -> int:
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    os.fchmod(descriptor, 0o600)
    return descriptor


@contextmanager
def job_lease(
    jobs_root: Path, job_id: str, *, blocking: bool = True
) -> Iterator[None]:
    """Hold the cross-process lease used by execution and terminal cleanup."""
    if not JOB_ID_PATTERN.fullmatch(job_id):
        raise ValueError("job_id has an invalid format")
    root = _resolved(jobs_root)
    leases = root / ".leases"
    _private_directory(leases)
    descriptor = _open_lock(leases / f"{job_id}.lock")
    operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
    try:
        try:
            fcntl.flock(descriptor, operation)
        except BlockingIOError as error:
            raise JobLeaseBusy(job_id) from error
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


@contextmanager
def _retention_lease(jobs_root: Path) -> Iterator[bool]:
    descriptor = _open_lock(jobs_root / ".retention.lock")
    acquired = False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            pass
        yield acquired
    finally:
        if acquired:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise RetentionSafetyError("manifest has an invalid terminal timestamp") from error
    if parsed.tzinfo is None:
        raise RetentionSafetyError("manifest terminal timestamp has no timezone")
    return parsed.astimezone(UTC)


def terminal_time(manifest: JobManifest) -> datetime:
    value = getattr(manifest, "terminal_at", None) or manifest.updated_at
    return _timestamp(value)


def is_expired_terminal(manifest: JobManifest, cutoff: datetime) -> bool:
    return manifest.state in TERMINAL_STATES and terminal_time(manifest) <= cutoff


def _alias(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold().removesuffix(".git"))


def _direct_child(root: Path, name: str) -> Path:
    if not JOB_ID_PATTERN.fullmatch(name):
        raise RetentionSafetyError("job_id has an invalid format")
    child = root / name
    if child.parent != root:
        raise RetentionSafetyError("cleanup target is not a direct child")
    return child


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_no_follow(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if path.is_symlink() or not path.is_dir():
        path.unlink()
        return
    # CPython uses its fd-based implementation on the deployed Ubuntu host;
    # explicitly fail closed on a platform where rmtree follows symlinks.
    if not shutil.rmtree.avoids_symlink_attacks:
        raise RetentionSafetyError("safe recursive deletion is unavailable")
    del metadata
    shutil.rmtree(path)


def _parse_worktree_list(output: str) -> tuple[dict[str, str], ...]:
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for token in output.split("\x00"):
        if not token:
            if current:
                records.append(current)
                current = {}
            continue
        key, separator, value = token.partition(" ")
        current[key] = value if separator else ""
    if current:
        records.append(current)
    return tuple(records)


class RetentionReaper:
    """Remove expired terminal jobs without trusting paths in their manifests."""

    def __init__(
        self,
        store: ManifestStore,
        *,
        workspace: Path,
        worktrees_root: Path,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        git_executable: str = "git",
        runner: CommandRunner | None = None,
        git_timeout_seconds: float = DEFAULT_GIT_CLEANUP_TIMEOUT_SECONDS,
        clock: Callable[[], datetime] | None = None,
        phase_hook: Callable[[str, JobManifest], None] | None = None,
    ) -> None:
        if retention_days <= 0:
            raise ValueError("retention_days must be positive")
        if git_timeout_seconds <= 0:
            raise ValueError("git_timeout_seconds must be positive")
        workspace_root, worktrees, jobs = validate_retention_roots(
            workspace=workspace,
            worktrees_root=worktrees_root,
            jobs_root=store.root,
        )
        self.store = store
        self.workspace = workspace_root
        self.worktrees_root = worktrees
        self.jobs_root = jobs
        self.retention_days = retention_days
        self.git = git_executable
        self.runner = runner or SubprocessCommandRunner()
        self.git_timeout_seconds = git_timeout_seconds
        self._clock = clock or (lambda: datetime.now(UTC))
        self._phase_hook = phase_hook or (lambda _phase, _manifest: None)

    def prune_once(
        self,
        *,
        now: datetime | None = None,
        limit: int = DEFAULT_RETENTION_BATCH_SIZE,
    ) -> RetentionReport:
        if limit <= 0:
            raise ValueError("limit must be positive")
        timestamp = now or self._clock()
        if timestamp.tzinfo is None:
            raise ValueError("now must include a timezone")
        cutoff = timestamp.astimezone(UTC) - timedelta(days=self.retention_days)

        with _retention_lease(self.jobs_root) as acquired:
            if not acquired:
                return RetentionReport(0, 0, (), (), False)
            scan = self.store.scan()
            issues = [
                RetentionIssue(failure.job_id, "manifest", failure.detail)
                for failure in scan.failures
            ]
            candidates: list[JobManifest] = []
            for manifest in scan.manifests:
                try:
                    if is_expired_terminal(manifest, cutoff):
                        candidates.append(manifest)
                except RetentionSafetyError as error:
                    issues.append(
                        RetentionIssue(manifest.job_id, "manifest", _safe_detail(error))
                    )
            candidates.sort(key=lambda item: (terminal_time(item), item.job_id))
            selected = candidates[:limit]
            deleted: list[str] = []
            for manifest in selected:
                try:
                    with job_lease(self.jobs_root, manifest.job_id, blocking=False):
                        current = self.store.get(manifest.job_id)
                        if (
                            current.payload_hash != manifest.payload_hash
                            or not is_expired_terminal(current, cutoff)
                        ):
                            continue
                        self._cleanup_worktree(current)
                        self._phase_hook("worktree", current)
                        self._cleanup_artifacts(current)
                        self._phase_hook("artifacts", current)
                        self._cleanup_job_lease_file(current.job_id)
                        removed = self.store.delete_if_expired_terminal(
                            current.job_id,
                            payload_hash=current.payload_hash,
                            cutoff=cutoff,
                        )
                        if not removed:
                            raise RetentionSafetyError(
                                "manifest changed before conditional deletion"
                            )
                        deleted.append(current.job_id)
                        self._phase_hook("manifest", current)
                except JobLeaseBusy:
                    issues.append(
                        RetentionIssue(
                            manifest.job_id,
                            "process",
                            "job is still owned by a worker process",
                        )
                    )
                except ManifestNotFoundError:
                    # A concurrent cleanup completed the same idempotent operation.
                    continue
                except Exception as error:
                    issues.append(
                        RetentionIssue(
                            manifest.job_id,
                            "cleanup",
                            _safe_detail(error),
                        )
                    )

            return RetentionReport(
                scanned=len(scan.manifests) + len(scan.failures),
                eligible=len(candidates),
                deleted=tuple(deleted),
                deferred=tuple(issues),
                has_more=len(candidates) > len(selected),
            )

    def _cleanup_worktree(self, manifest: JobManifest) -> None:
        target = _direct_child(self.worktrees_root, manifest.job_id)
        if target.is_symlink():
            raise RetentionSafetyError("worktree target is a symlink")
        target_exists = target.exists()
        if target_exists and not target.is_dir():
            raise RetentionSafetyError("worktree target is not a directory")

        branch = manifest.branch
        if branch is not None and not branch.startswith(
            f"poo-ia/{manifest.job_id}-"
        ):
            raise RetentionSafetyError("manifest branch is not owned by this job")
        if not target_exists and branch is None:
            return

        repository = self._find_repository(manifest, target if target_exists else None)
        if repository is None:
            if target_exists or branch is not None:
                raise RetentionSafetyError("could not prove the owning repository")
            return

        records = self._worktree_records(repository)
        target_records = [
            record
            for record in records
            if record.get("worktree")
            and _resolved(Path(record["worktree"])) == target
        ]
        if len(target_records) > 1:
            raise RetentionSafetyError("worktree has ambiguous Git registrations")
        record = target_records[0] if target_records else None

        if target_exists:
            if branch is None:
                raise RetentionSafetyError("worktree is missing its owned branch")
            top = self._git(target, "rev-parse", "--show-toplevel")
            if _resolved(Path(top.stdout.strip())) != target:
                raise RetentionSafetyError("worktree root does not match cleanup target")
            actual_branch = self._git(target, "branch", "--show-current").stdout.strip()
            if actual_branch != branch:
                raise RetentionSafetyError("worktree branch does not match its manifest")
            if record is None or record.get("branch") != f"refs/heads/{branch}":
                raise RetentionSafetyError("worktree Git registration does not match")
            target_common = self._common_git_directory(target)
            repository_common = self._common_git_directory(repository)
            if target_common != repository_common:
                raise RetentionSafetyError("worktree belongs to a different repository")

        if record is not None:
            removed = self._git(
                repository,
                "worktree",
                "remove",
                "--force",
                str(target),
                check=False,
            )
            if removed.returncode != 0:
                raise RetentionSafetyError("Git could not remove the owned worktree")
        if target.exists() or target.is_symlink():
            raise RetentionSafetyError("owned worktree still exists after Git removal")
        if any(
            record.get("worktree")
            and _resolved(Path(record["worktree"])) == target
            for record in self._worktree_records(repository)
        ):
            raise RetentionSafetyError("Git still registers the removed worktree")

        if branch is not None:
            branch_ref = f"refs/heads/{branch}"
            if any(record.get("branch") == branch_ref for record in self._worktree_records(repository)):
                raise RetentionSafetyError("owned branch is checked out in another worktree")
            exists = self._git(
                repository,
                "show-ref",
                "--verify",
                "--quiet",
                branch_ref,
                check=False,
            )
            if exists.returncode == 0:
                deleted = self._git(
                    repository, "branch", "-D", branch, check=False
                )
                if deleted.returncode != 0:
                    raise RetentionSafetyError("Git could not delete the owned local branch")
            elif exists.returncode != 1:
                raise RetentionSafetyError("Git could not inspect the owned local branch")

    def _find_repository(
        self, manifest: JobManifest, target: Path | None
    ) -> Path | None:
        repositories = self._repository_inventory()
        if target is not None:
            try:
                common = self._common_git_directory(target)
            except RetentionSafetyError:
                common = None
            if common is not None:
                matches = [
                    repository
                    for repository in repositories
                    if self._common_git_directory(repository) == common
                ]
                if len(matches) == 1:
                    return matches[0]
                if len(matches) > 1:
                    raise RetentionSafetyError("worktree repository is ambiguous")

        recorded = getattr(manifest, "repo_path", None)
        if recorded:
            recorded_path = _resolved(Path(recorded))
            matches = [path for path in repositories if path == recorded_path]
            if len(matches) == 1:
                return matches[0]

        exact = [
            path
            for path in repositories
            if path.name.casefold() == manifest.repository.casefold()
        ]
        matches = exact or [
            path for path in repositories if _alias(path.name) == _alias(manifest.repository)
        ]
        if len(matches) > 1:
            raise RetentionSafetyError("manifest repository alias is ambiguous")
        return matches[0] if matches else None

    def _repository_inventory(self) -> tuple[Path, ...]:
        if not self.workspace.is_dir() or self.workspace.is_symlink():
            raise RetentionSafetyError("workspace is unavailable")
        repositories: list[Path] = []
        for child in self.workspace.iterdir():
            if child.is_symlink() or not child.is_dir():
                continue
            marker = child / ".git"
            if marker.is_dir() or marker.is_file():
                resolved = child.resolve(strict=True)
                if resolved.parent == self.workspace:
                    repositories.append(resolved)
        return tuple(sorted(repositories, key=lambda path: path.name.casefold()))

    def _common_git_directory(self, path: Path) -> Path:
        result = self._git(path, "rev-parse", "--git-common-dir", check=False)
        if result.returncode != 0 or not result.stdout.strip():
            raise RetentionSafetyError("could not resolve Git common directory")
        common = Path(result.stdout.strip())
        if not common.is_absolute():
            common = path / common
        return common.resolve(strict=False)

    def _worktree_records(self, repository: Path) -> tuple[dict[str, str], ...]:
        result = self._git(
            repository, "worktree", "list", "--porcelain", "-z", check=False
        )
        if result.returncode != 0:
            raise RetentionSafetyError("Git could not list repository worktrees")
        return _parse_worktree_list(result.stdout)

    def _git(
        self, cwd: Path, *arguments: str, check: bool = True
    ) -> CommandResult:
        try:
            result = self.runner.run(
                (self.git, *arguments),
                cwd=cwd,
                timeout=self.git_timeout_seconds,
            )
        except OSError as error:
            raise RetentionSafetyError("Git is unavailable during cleanup") from error
        if check and result.returncode != 0:
            raise RetentionSafetyError("Git cleanup verification failed")
        return result

    def _cleanup_artifacts(self, manifest: JobManifest) -> None:
        artifacts = _direct_child(self.jobs_root, manifest.job_id)
        trash_root = self.jobs_root / ".trash"
        _private_directory(trash_root)
        trash = _direct_child(trash_root, manifest.job_id)

        if trash.exists() or trash.is_symlink():
            _remove_no_follow(trash)
        if artifacts.exists() or artifacts.is_symlink():
            os.replace(artifacts, trash)
            _fsync_directory(self.jobs_root)
            _fsync_directory(trash_root)
        _remove_no_follow(trash)
        _fsync_directory(trash_root)

    def _cleanup_job_lease_file(self, job_id: str) -> None:
        leases = self.jobs_root / ".leases"
        lease = leases / f"{job_id}.lock"
        if lease.parent != leases:
            raise RetentionSafetyError("job lease is not a direct child")
        if lease.is_symlink():
            raise RetentionSafetyError("job lease is a symlink")
        try:
            lease.unlink()
        except FileNotFoundError:
            return
        _fsync_directory(leases)
