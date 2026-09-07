"""Idempotent GitHub publication for prepared worker jobs."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .config import WorkerSettings
from .models import JobManifest, JobState, ValidationStatus
from .processes import (
    CommandResult,
    CommandRunner,
    CommandTimedOut,
    SubprocessCommandRunner,
)
from .store import ManifestStateError, ManifestStore
from .validation import Validator
from .validation_sandbox import DockerValidationRunner


class PublicationError(RuntimeError):
    """A prepared change could not be published safely."""


PULL_REQUEST_URL = re.compile(r"^https://github\.com/[^/]+/[^/]+/pull/\d+/?$")


class GitHubPublisher:
    def __init__(
        self,
        settings: WorkerSettings,
        store: ManifestStore,
        *,
        runner: CommandRunner | None = None,
        validator: Validator | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.runner = runner or SubprocessCommandRunner()
        self.validator = validator or Validator(
            git_executable=settings.git_executable,
            runner=self.runner,
            timeout_seconds=settings.validation_timeout_seconds,
            enabled=settings.validation_enabled,
            test_runner=(
                DockerValidationRunner(
                    staging_root=settings.data_root / "validation",
                    docker_executable=settings.docker_executable,
                    build_timeout_seconds=settings.validation_build_timeout_seconds,
                )
                if settings.validation_enabled else None
            ),
        )

    def publish(
        self,
        job_id: str,
        *,
        override: bool = False,
        process_pid: int | None = None,
    ) -> JobManifest:
        manifest = self.store.get(job_id)
        if manifest.pr_url:
            return manifest
        if manifest.state is not JobState.PREPARED:
            raise PublicationError(
                f"job {job_id} is {manifest.state.value}, not prepared"
            )
        self._require_common_metadata(manifest)

        self.store.update(
            job_id,
            expected=(JobState.PREPARED,),
            transform=lambda current: current.evolve(
                state=JobState.PUBLISHING,
                process_pid=process_pid,
                error=None,
            ),
        )
        return self.publish_claimed(job_id, override=override, process_pid=process_pid)

    def publish_claimed(
        self,
        job_id: str,
        *,
        override: bool = False,
        process_pid: int | None = None,
    ) -> JobManifest:
        """Continue a durable publication whose manifest is already claimed."""
        manifest = self.store.get(job_id)
        if manifest.pr_url:
            return manifest
        if manifest.state is not JobState.PUBLISHING:
            raise PublicationError(
                f"job {job_id} is {manifest.state.value}, not publishing"
            )
        if process_pid is not None:

            def claim(current: JobManifest) -> JobManifest:
                if current.process_pid not in {None, process_pid}:
                    raise ManifestStateError(
                        f"job {job_id} is owned by another publication process"
                    )
                return current.evolve(process_pid=process_pid)

            manifest = self.store.update(
                job_id,
                expected=(JobState.PUBLISHING,),
                transform=claim,
            )

        try:
            return self._publish_claimed(manifest, override=override)
        except Exception as error:
            safe = " ".join(str(error).split())[:500] or error.__class__.__name__
            try:
                self.store.update(
                    job_id,
                    expected=(JobState.PUBLISHING,),
                    transform=lambda current: current.evolve(
                        state=JobState.PREPARED,
                        process_pid=None,
                        error=safe,
                    ),
                )
            except ManifestStateError:
                pass
            if isinstance(error, PublicationError):
                raise
            raise PublicationError(safe) from error

    def _publish_claimed(self, manifest: JobManifest, *, override: bool) -> JobManifest:
        self._require_common_metadata(manifest)
        assert manifest.worktree is not None
        assert manifest.branch is not None
        worktree = Path(manifest.worktree)

        try:
            self._require_success(
                self.runner.run(
                    (self.settings.gh_executable, "auth", "status"),
                    cwd=worktree,
                    timeout=self.settings.github_timeout_seconds,
                ),
                "GitHub CLI is not authenticated",
            )
            self._require_publishable_base(manifest)
            self._check_gate(manifest, override=override)

            # Check that the remote is inspectable before creating a local commit.
            # Its value is checked again after validation because the ref is mutable.
            self._remote_branch_sha(worktree, manifest.branch)

            head = self._resolve_head(worktree)
            if head == manifest.base_commit.lower():
                manifest = self._refresh_validation_and_diff(manifest, worktree)
                self._check_gate(manifest, override=override)
                self._require_head_is_base(manifest, worktree)

                status = self._git(
                    worktree, "status", "--porcelain=v1", "--untracked-files=all"
                )
                if not status.stdout.strip():
                    raise PublicationError(
                        "the prepared change is empty; publication is blocked"
                    )
                self._require_success(
                    self._git(worktree, "add", "-A"),
                    "could not stage the prepared change",
                )
                cached = self._git(worktree, "diff", "--cached", "--quiet", check=False)
                if cached.returncode not in {0, 1}:
                    raise PublicationError("could not inspect the staged change")
                if cached.returncode == 0:
                    raise PublicationError(
                        "the prepared change is empty; publication is blocked"
                    )

                # This check sits immediately before `git commit`. A concurrent
                # history change is also caught by the parent check afterwards.
                self._require_head_is_base(manifest, worktree)
                self._require_success(
                    self._git(
                        worktree,
                        "commit",
                        "-m",
                        self._title(manifest),
                        "-m",
                        self._ownership_trailer(manifest),
                    ),
                    "could not commit the prepared change",
                )
                commit_sha = self._verified_prepared_commit(manifest, worktree)
            else:
                # The only accepted non-base HEAD is the exact commit this
                # publisher made for this job during an interrupted attempt.
                commit_sha = self._verified_prepared_commit(manifest, worktree)
                manifest = self._refresh_validation_and_diff(manifest, worktree)
                self._check_gate(manifest, override=override)
                commit_sha = self._verified_prepared_commit(manifest, worktree)

            remote_sha = self._remote_branch_sha(worktree, manifest.branch)
            if remote_sha != commit_sha:
                # Push the immutable object ID, never the mutable local branch ref.
                self._require_success(
                    self._git(
                        worktree,
                        "push",
                        "origin",
                        f"{commit_sha}:refs/heads/{manifest.branch}",
                        check=False,
                    ),
                    "could not push the prepared branch",
                )
            self._require_remote_commit(worktree, manifest.branch, commit_sha)

            existing = self._existing_pr(worktree, manifest.branch)
            if existing:
                # The PR lookup is not authoritative for the branch target. Check
                # the remote again before adopting an existing PR into the job.
                self._require_remote_commit(worktree, manifest.branch, commit_sha)
                return self._succeed(manifest.job_id, existing)

            body = self._body(manifest)
            created = self.runner.run(
                (
                    self.settings.gh_executable,
                    "pr",
                    "create",
                    "--head",
                    manifest.branch,
                    "--base",
                    manifest.base_branch,
                    "--title",
                    self._title(manifest),
                    "--body",
                    body,
                ),
                cwd=worktree,
                timeout=self.settings.github_timeout_seconds,
            )
            self._require_success(created, "GitHub did not create the pull request")
            url = self._extract_url(created.stdout)
            self._require_remote_commit(worktree, manifest.branch, commit_sha)
            if not url:
                url = self._existing_pr(worktree, manifest.branch)
                if url:
                    self._require_remote_commit(
                        worktree, manifest.branch, commit_sha
                    )
            if not url:
                raise PublicationError(
                    "GitHub did not return a verifiable pull request URL"
                )
            return self._succeed(manifest.job_id, url)
        except (OSError, CommandTimedOut) as error:
            raise PublicationError(str(error)) from error

    def _refresh_validation_and_diff(
        self, manifest: JobManifest, worktree: Path
    ) -> JobManifest:
        if not manifest.repo_path or not manifest.base_commit:
            raise PublicationError("prepared job is missing validation metadata paths")
        validation = self.validator.validate(
            worktree,
            base_repository=Path(manifest.repo_path),
            base_commit=manifest.base_commit,
            job_directory=self.store.root / manifest.job_id,
        )
        measurement = self.validator.measure_diff(
            worktree,
            base_commit=manifest.base_commit,
            max_files=self.settings.max_changed_files,
            max_lines=self.settings.max_changed_lines,
        )
        current_fingerprint = self._diff_fingerprint(manifest, worktree)
        if not manifest.diff_sha256:
            raise PublicationError(
                "prepared job has no diff fingerprint; prepare the change again"
            )
        if current_fingerprint != manifest.diff_sha256:
            raise PublicationError(
                "the worktree changed since it was prepared; publication is blocked"
            )
        return self.store.update(
            manifest.job_id,
            expected=(JobState.PUBLISHING,),
            transform=lambda current: current.evolve(
                validation=validation,
                diff=measurement,
                error=None,
            ),
        )

    def _verified_prepared_commit(self, manifest: JobManifest, worktree: Path) -> str:
        """Return HEAD only when it is the publisher-owned commit for this job."""
        commit_sha = self._resolve_head(worktree)
        parents = self._git(
            worktree, "rev-list", "--parents", "-n", "1", commit_sha
        ).stdout.split()
        if (
            len(parents) != 2
            or parents[0].lower() != commit_sha
            or parents[1].lower() != manifest.base_commit.lower()
        ):
            raise PublicationError(
                "the prepared commit parent is not the prepared base"
            )

        message = self._git(
            worktree, "show", "-s", "--format=%B", commit_sha
        ).stdout
        expected_trailer = self._ownership_trailer(manifest)
        # Strip only the record terminator emitted by `git show`; whitespace in
        # the trailer itself is significant and must not turn a near-match into
        # an ownership marker.
        message_lines = message.rstrip("\r\n").splitlines()
        if (
            not message_lines
            or message_lines[-1] != expected_trailer
            or message_lines.count(expected_trailer) != 1
        ):
            raise PublicationError(
                "the prepared commit was not created by the publisher for this job"
            )

        status = self._git(
            worktree, "status", "--porcelain=v1", "--untracked-files=all"
        )
        if status.stdout.strip():
            raise PublicationError(
                "the worktree changed during publication; push is blocked"
            )
        current = self._diff_fingerprint(manifest, worktree, target_commit=commit_sha)
        if not manifest.diff_sha256 or current != manifest.diff_sha256:
            raise PublicationError(
                "the worktree changed during publication; push is blocked"
            )
        return commit_sha

    def _resolve_head(self, worktree: Path) -> str:
        head = self._git(worktree, "rev-parse", "--verify", "HEAD^{commit}")
        commit_sha = head.stdout.strip()
        if not re.fullmatch(r"[0-9a-fA-F]{40,64}", commit_sha):
            raise PublicationError("could not resolve the exact prepared commit")
        return commit_sha.lower()

    def _require_head_is_base(
        self, manifest: JobManifest, worktree: Path
    ) -> None:
        if self._resolve_head(worktree) != manifest.base_commit.lower():
            raise PublicationError(
                "HEAD changed before publication; no commit was created"
            )

    def _remote_branch_sha(self, worktree: Path, branch: str) -> str | None:
        remote_ref = f"refs/heads/{branch}"
        result = self._git(
            worktree,
            "ls-remote",
            "--heads",
            "origin",
            remote_ref,
            check=False,
        )
        if result.returncode != 0:
            raise PublicationError("could not inspect the remote publication branch")
        if not result.stdout.strip():
            return None

        lines = [line.split() for line in result.stdout.splitlines() if line.strip()]
        if (
            len(lines) != 1
            or len(lines[0]) != 2
            or lines[0][1] != remote_ref
            or not re.fullmatch(r"[0-9a-fA-F]{40,64}", lines[0][0])
        ):
            raise PublicationError(
                "the remote publication branch returned an invalid commit"
            )
        return lines[0][0].lower()

    def _require_remote_commit(
        self, worktree: Path, branch: str, commit_sha: str
    ) -> None:
        if self._remote_branch_sha(worktree, branch) != commit_sha:
            raise PublicationError(
                "the remote publication branch does not match the prepared commit"
            )

    @staticmethod
    def _ownership_trailer(manifest: JobManifest) -> str:
        return f"Poo-IA-Job: {manifest.job_id}"

    def _diff_fingerprint(
        self,
        manifest: JobManifest,
        worktree: Path,
        *,
        target_commit: str | None = None,
    ) -> str:
        assert manifest.base_commit is not None
        if target_commit is None:
            untracked = self._git(
                worktree,
                "ls-files",
                "--others",
                "--exclude-standard",
                "-z",
            )
            paths = tuple(filter(None, untracked.stdout.split("\x00")))
            if paths:
                self._require_success(
                    self._git(worktree, "add", "--intent-to-add", "--", *paths),
                    "could not prepare new files for publication verification",
                )
        arguments = ["diff", "--binary", manifest.base_commit]
        if target_commit is not None:
            arguments.append(target_commit)
        rendered = self._git(
            worktree,
            *arguments,
        )
        return (
            rendered.stdout_sha256
            or hashlib.sha256(
                rendered.stdout.encode("utf-8", errors="replace")
            ).hexdigest()
        )

    @staticmethod
    def _require_common_metadata(manifest: JobManifest) -> None:
        from app.repository_scope import is_documentation_repository
        if is_documentation_repository(manifest.repository) or (
            manifest.repo_path and is_documentation_repository(Path(manifest.repo_path).name)
        ):
            raise PublicationError("El brain es documental y no puede recibir el PR de ejecución.")
        if not manifest.worktree or not manifest.branch or not manifest.base_commit:
            raise PublicationError("prepared job is missing Git worktree metadata")

    @staticmethod
    def _require_publishable_base(manifest: JobManifest) -> None:
        if not manifest.base_branch:
            raise PublicationError(
                "prepared job is missing its base branch; prepare the change again"
            )
        if manifest.base_branch == "detached":
            raise PublicationError(
                "a change prepared from detached HEAD cannot create a pull request"
            )

    def _existing_pr(self, worktree: Path, branch: str) -> str | None:
        result = self.runner.run(
            (
                self.settings.gh_executable,
                "pr",
                "view",
                branch,
                "--json",
                "url",
                "--jq",
                ".url",
            ),
            cwd=worktree,
            timeout=self.settings.github_timeout_seconds,
        )
        if result.returncode != 0:
            return None
        return self._extract_url(result.stdout)

    def _succeed(self, job_id: str, url: str) -> JobManifest:
        from .documentation import record_process
        documentation = record_process(self.settings, self.store.get(job_id).evolve(
            state=JobState.SUCCEEDED, pr_url=url, error=None,
        ))
        return self.store.update(
            job_id,
            expected=(JobState.PUBLISHING,),
            transform=lambda current: current.evolve(
                state=JobState.SUCCEEDED,
                process_pid=None,
                pr_url=url,
                error=None,
                documentation=documentation,
            ),
        )

    @staticmethod
    def _extract_url(output: str) -> str | None:
        for token in output.split():
            candidate = token.strip()
            if PULL_REQUEST_URL.fullmatch(candidate):
                return candidate.rstrip("/")
        return None

    @staticmethod
    def _check_gate(manifest: JobManifest, *, override: bool) -> None:
        if manifest.diff is None or manifest.validation is None:
            raise PublicationError("the prepared job is missing validation metadata")
        if manifest.diff and manifest.diff.over_budget and not override:
            raise PublicationError(
                "the prepared diff exceeds its automatic publication budget"
            )
        if (
            manifest.validation
            and manifest.validation.status
            in {ValidationStatus.FAILED, ValidationStatus.TIMED_OUT}
            and not override
        ):
            raise PublicationError(
                f"validation is {manifest.validation.status.value}; explicit override is required"
            )

    @staticmethod
    def _title(manifest: JobManifest) -> str:
        first_line = next(
            (line.strip() for line in manifest.prompt.splitlines() if line.strip()),
            "Prepare repository change",
        )
        clean = re.sub(r"\s+", " ", first_line)
        return f"Poo-IA: {clean}"[:72].rstrip()

    @staticmethod
    def _body(manifest: JobManifest) -> str:
        validation = (
            manifest.validation.status.value if manifest.validation else "unavailable"
        )
        diff = manifest.diff
        measurement = (
            f"{diff.changed_files} files, {diff.changed_lines} changed lines"
            if diff
            else "measurement unavailable"
        )
        summary = (manifest.summary or "Change prepared by Codex.")[:4_000]
        return (
            f"{summary}\n\n"
            f"Validation: `{validation}`\n\n"
            f"Diff budget: {measurement}\n\n"
            f"Poo-IA job: `{manifest.job_id}`"
        )

    def _git(self, cwd: Path, *args: str, check: bool = True) -> CommandResult:
        result = self.runner.run(
            (self.settings.git_executable, *args),
            cwd=cwd,
            timeout=self.settings.github_timeout_seconds,
        )
        if check and result.returncode != 0:
            raise PublicationError("git publication operation failed")
        return result

    @staticmethod
    def _require_success(result: CommandResult, message: str) -> None:
        if result.returncode != 0:
            raise PublicationError(message)
