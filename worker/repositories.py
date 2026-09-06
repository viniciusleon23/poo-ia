"""Safe repository discovery and isolated Git worktree creation."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path

from .models import JOB_ID_PATTERN
from .processes import CommandResult, CommandRunner, SubprocessCommandRunner


class RepositoryError(RuntimeError):
    """A repository could not be resolved or safely prepared."""


def _alias(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold().removesuffix(".git"))


def _slug(value: str, maximum: int = 32) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return (slug or "change")[:maximum].rstrip("-")


@dataclass(frozen=True, slots=True)
class RepositorySnapshot:
    name: str
    path: Path
    base_commit: str
    base_branch: str


@dataclass(frozen=True, slots=True)
class PreparedWorktree:
    repository: RepositorySnapshot
    path: Path
    branch: str


class RepositoryResolver:
    def __init__(
        self,
        workspace: Path,
        worktrees_root: Path,
        *,
        git_executable: str = "git",
        runner: CommandRunner | None = None,
    ) -> None:
        self.workspace = workspace.resolve()
        self.worktrees_root = worktrees_root.resolve()
        self.git = git_executable
        self.runner = runner or SubprocessCommandRunner()

    def inventory(self) -> tuple[Path, ...]:
        if not self.workspace.is_dir():
            raise RepositoryError("CAPNET_WORKSPACE does not exist or is not a directory")
        repositories: list[Path] = []
        for child in sorted(self.workspace.iterdir(), key=lambda path: path.name.casefold()):
            if not child.is_dir() or child.is_symlink():
                continue
            git_marker = child / ".git"
            if git_marker.is_dir() or git_marker.is_file():
                repositories.append(child.resolve())
        return tuple(repositories)

    def clean_names(self) -> tuple[str, ...]:
        """Return only direct repositories whose base checkout is clean."""
        names: list[str] = []
        for repository in self.inventory():
            try:
                self._ensure_clean(repository)
            except RepositoryError:
                continue
            names.append(repository.name)
        return tuple(names)

    def resolve(self, requested: str) -> RepositorySnapshot:
        name = requested.strip()
        if (
            not name
            or name in {".", ".."}
            or "/" in name
            or "\\" in name
            or "\x00" in name
        ):
            raise RepositoryError("repository must be a direct workspace repository name")

        repositories = self.inventory()
        exact = [path for path in repositories if path.name.casefold() == name.casefold()]
        matches = exact or [path for path in repositories if _alias(path.name) == _alias(name)]
        if not matches:
            raise RepositoryError(f"repository {name!r} was not found in CAPNET_WORKSPACE")
        if len(matches) != 1:
            options = ", ".join(path.name for path in matches)
            raise RepositoryError(f"repository alias {name!r} is ambiguous: {options}")

        path = matches[0]
        self._ensure_clean(path)
        base_commit = self._git(path, "rev-parse", "HEAD").stdout.strip()
        branch_result = self._git(path, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
        base_branch = branch_result.stdout.strip() if branch_result.returncode == 0 else "detached"
        if not re.fullmatch(r"[0-9a-fA-F]{40,64}", base_commit):
            raise RepositoryError(f"repository {path.name} has an invalid HEAD")
        return RepositorySnapshot(path.name, path, base_commit, base_branch)

    def prepare(self, snapshot: RepositorySnapshot, job_id: str, prompt: str) -> PreparedWorktree:
        if not JOB_ID_PATTERN.fullmatch(job_id):
            raise RepositoryError("job_id has an invalid format")
        self.worktrees_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = self.worktrees_root / job_id
        branch = f"poo-ia/{job_id}-{_slug(prompt)}"

        if target.exists():
            if target.is_symlink() or not target.is_dir():
                raise RepositoryError("existing worktree target is not a directory")
            actual_root = self._git(target, "rev-parse", "--show-toplevel").stdout.strip()
            actual_branch = self._git(target, "branch", "--show-current").stdout.strip()
            if Path(actual_root).resolve() != target.resolve() or actual_branch != branch:
                raise RepositoryError("existing job worktree does not match its manifest")
            status = self._git(
                target, "status", "--porcelain=v1", "--untracked-files=all"
            ).stdout
            if status.strip():
                raise RepositoryError(
                    "existing worktree contains unconfirmed changes; automatic rerun is blocked"
                )
            worktree_head = self._git(target, "rev-parse", "HEAD").stdout.strip()
            resumed_snapshot = replace(snapshot, base_commit=worktree_head)
            return PreparedWorktree(resumed_snapshot, target.resolve(), branch)

        branch_ref = f"refs/heads/{branch}"
        branch_exists = self._git(
            snapshot.path,
            "show-ref",
            "--verify",
            "--quiet",
            branch_ref,
            check=False,
        ).returncode == 0
        if branch_exists:
            self._git(snapshot.path, "worktree", "add", str(target), branch)
        else:
            self._git(
                snapshot.path,
                "worktree",
                "add",
                "-b",
                branch,
                str(target),
                snapshot.base_commit,
            )
        return PreparedWorktree(snapshot, target.resolve(), branch)

    def interrupted_retry_is_safe(
        self,
        *,
        job_id: str,
        worktree: str | None,
        branch: str | None,
        base_commit: str | None,
    ) -> bool:
        """Allow recovery only when no prior execution could have changed the tree."""
        if not JOB_ID_PATTERN.fullmatch(job_id):
            return False
        target = self.worktrees_root / job_id
        if target.is_symlink():
            return False
        if not target.exists():
            # A crash before worktree creation has no repository effects to replay.
            return worktree is None and branch is None and base_commit is None
        if (
            not target.is_dir()
            or worktree is None
            or branch is None
            or base_commit is None
            or Path(worktree).resolve(strict=False) != target.resolve(strict=False)
        ):
            return False
        try:
            actual_root = self._git(target, "rev-parse", "--show-toplevel").stdout.strip()
            actual_branch = self._git(target, "branch", "--show-current").stdout.strip()
            actual_head = self._git(target, "rev-parse", "HEAD").stdout.strip()
            status = self._git(
                target, "status", "--porcelain=v1", "--untracked-files=all"
            ).stdout
        except RepositoryError:
            return False
        return bool(
            Path(actual_root).resolve(strict=False) == target.resolve(strict=False)
            and actual_branch == branch
            and actual_head == base_commit
            and not status.strip()
        )

    def _ensure_clean(self, path: Path) -> None:
        status = self._git(path, "status", "--porcelain=v1", "--untracked-files=all").stdout
        if status.strip():
            raise RepositoryError(
                f"repository {path.name} has local changes; its base checkout must be clean"
            )

    def _git(self, cwd: Path, *arguments: str, check: bool = True) -> CommandResult:
        try:
            result = self.runner.run((self.git, *arguments), cwd=cwd)
        except OSError as error:
            raise RepositoryError("git is unavailable on the worker host") from error
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().splitlines()
            message = detail[-1][:500] if detail else "unknown git error"
            raise RepositoryError(f"git operation failed: {message}")
        return result
