from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from worker.models import JobRequest, JobState
from worker.repositories import RepositoryResolver
from worker.retention import (
    JobLeaseBusy,
    RetentionReaper,
    RetentionSafetyError,
    job_lease,
    validate_retention_roots,
)
from worker.store import ManifestNotFoundError, ManifestStore


def git(cwd: Path, *arguments: str, check: bool = True) -> str:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=cwd,
        text=True,
        capture_output=True,
        check=check,
    )
    return completed.stdout.strip()


def initialize_repository(workspace: Path, name: str = "repo") -> Path:
    repository = workspace / name
    repository.mkdir(parents=True)
    git(repository, "init", "-q")
    git(repository, "config", "user.email", "tests@example.invalid")
    git(repository, "config", "user.name", "Retention Tests")
    (repository / "README.md").write_text("base\n", encoding="utf-8")
    git(repository, "add", "README.md")
    git(repository, "commit", "-qm", "base")
    return repository


class SimulatedCrash(BaseException):
    pass


class RetentionReaperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.repository = initialize_repository(self.workspace)
        self.worktrees = self.root / "worktrees"
        self.jobs = self.root / "data" / "jobs"
        self.store = ManifestStore(self.jobs)
        self.now = datetime(2026, 9, 6, 12, tzinfo=UTC)
        self.old = self.now - timedelta(days=31)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def create_job(
        self,
        job_id: str,
        state: JobState,
        *,
        when: datetime | None = None,
        artifacts: bool = True,
    ):
        manifest, _ = self.store.create_or_get(
            JobRequest(job_id, "repo", f"change for {job_id}")
        )
        if artifacts:
            directory = self.jobs / job_id
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "worker.log").write_text("private output", encoding="utf-8")
        timestamp = (when or self.old).isoformat()
        if state is not JobState.QUEUED:
            manifest = self.store.update(
                job_id,
                expected=(JobState.QUEUED,),
                transform=lambda item: item.evolve(
                    state=state,
                    updated_at=timestamp,
                ),
            )
        return manifest

    def reaper(self, **kwargs) -> RetentionReaper:
        return RetentionReaper(
            self.store,
            workspace=self.workspace,
            worktrees_root=self.worktrees,
            **kwargs,
        )

    def test_only_expired_terminal_states_are_deleted(self) -> None:
        terminal_ids = []
        for index, state in enumerate(
            (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED), start=1
        ):
            job_id = f"terminal-{index}"
            terminal_ids.append(job_id)
            self.create_job(job_id, state)
        retained: dict[str, JobState] = {}
        for index, state in enumerate(
            (
                JobState.QUEUED,
                JobState.RUNNING,
                JobState.PREPARED,
                JobState.PUBLISHING,
            ),
            start=1,
        ):
            job_id = f"retained-{index}"
            retained[job_id] = state
            self.create_job(job_id, state)

        report = self.reaper().prune_once(now=self.now)

        self.assertEqual(set(report.deleted), set(terminal_ids))
        for job_id in terminal_ids:
            with self.assertRaises(ManifestNotFoundError):
                self.store.get(job_id)
            self.assertFalse((self.jobs / job_id).exists())
        for job_id, state in retained.items():
            self.assertEqual(self.store.get(job_id).state, state)
            self.assertTrue((self.jobs / job_id).exists())

    def test_exact_cutoff_is_inclusive_and_prepared_is_never_expired(self) -> None:
        cutoff = self.now - timedelta(days=30)
        self.create_job("exact-cutoff", JobState.SUCCEEDED, when=cutoff)
        self.create_job(
            "too-recent",
            JobState.SUCCEEDED,
            when=cutoff + timedelta(microseconds=1),
        )
        self.create_job("old-prepared", JobState.PREPARED, when=self.old)

        report = self.reaper().prune_once(now=self.now)

        self.assertEqual(report.deleted, ("exact-cutoff",))
        self.assertEqual(self.store.get("too-recent").state, JobState.SUCCEEDED)
        self.assertEqual(self.store.get("old-prepared").state, JobState.PREPARED)

    def test_real_worktree_artifacts_and_local_branch_are_removed_safely(self) -> None:
        job_id = "cleanup-real"
        request = JobRequest(job_id, "repo", "make a small change")
        manifest, _ = self.store.create_or_get(request)
        resolver = RepositoryResolver(self.workspace, self.worktrees)
        prepared = resolver.prepare(resolver.resolve("repo"), job_id, request.prompt)
        remote = self.root / "remote.git"
        git(self.root, "init", "--bare", "-q", str(remote))
        git(self.repository, "remote", "add", "origin", str(remote))
        git(prepared.path, "push", "-u", "origin", prepared.branch)
        (prepared.path / "change.txt").write_text("change\n", encoding="utf-8")

        outside = self.root / "outside"
        outside.mkdir()
        sentinel = outside / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")
        artifacts = self.jobs / job_id
        artifacts.mkdir()
        (artifacts / "change.diff").write_text("diff", encoding="utf-8")
        (artifacts / "outside-link").symlink_to(outside, target_is_directory=True)
        manifest = self.store.update(
            job_id,
            expected=(JobState.QUEUED,),
            transform=lambda item: item.evolve(
                state=JobState.FAILED,
                updated_at=self.old.isoformat(),
                repo_path=str(outside),
                worktree=str(outside),
                result_path=str(sentinel),
                branch=prepared.branch,
            ),
        )

        report = self.reaper().prune_once(now=self.now)

        self.assertEqual(report.deleted, (job_id,))
        self.assertFalse(prepared.path.exists())
        self.assertFalse(artifacts.exists())
        self.assertFalse((self.jobs / ".leases" / f"{job_id}.lock").exists())
        self.assertTrue(sentinel.exists())
        branches = git(self.repository, "branch", "--format=%(refname:short)").splitlines()
        self.assertNotIn(manifest.branch, branches)
        self.assertNotIn(str(prepared.path), git(self.repository, "worktree", "list"))
        self.assertEqual(
            git(remote, "show-ref", "--verify", f"refs/heads/{manifest.branch}"),
            f"{git(self.repository, 'rev-parse', 'HEAD')} refs/heads/{manifest.branch}",
        )

    def test_worktree_symlink_is_not_followed_and_evidence_is_retained(self) -> None:
        job_id = "unsafe-link"
        self.create_job(job_id, JobState.FAILED)
        outside = self.root / "outside-worktree"
        outside.mkdir()
        sentinel = outside / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")
        self.worktrees.mkdir()
        (self.worktrees / job_id).symlink_to(outside, target_is_directory=True)

        report = self.reaper().prune_once(now=self.now)

        self.assertEqual(report.deleted, ())
        self.assertTrue(report.deferred)
        self.assertTrue(sentinel.exists())
        self.assertTrue((self.jobs / f"{job_id}.json").exists())
        self.assertTrue((self.jobs / job_id).exists())

    def test_artifact_directory_symlink_is_unlinked_without_following(self) -> None:
        job_id = "artifact-link"
        self.create_job(job_id, JobState.CANCELLED, artifacts=False)
        outside = self.root / "outside-artifacts"
        outside.mkdir()
        sentinel = outside / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")
        (self.jobs / job_id).symlink_to(outside, target_is_directory=True)

        report = self.reaper().prune_once(now=self.now)

        self.assertEqual(report.deleted, (job_id,))
        self.assertTrue(sentinel.exists())
        self.assertFalse((self.jobs / job_id).exists())

    def test_corrupt_json_is_isolated_from_valid_cleanup(self) -> None:
        self.create_job("valid-old", JobState.CANCELLED)
        corrupt = self.jobs / "broken.json"
        corrupt.write_text("{not-json", encoding="utf-8")

        report = self.reaper().prune_once(now=self.now)

        self.assertEqual(report.deleted, ("valid-old",))
        self.assertTrue(corrupt.exists())
        self.assertTrue(
            any(issue.job_id == "broken" and issue.phase == "manifest" for issue in report.deferred)
        )

    def test_active_job_lease_defers_then_allows_cleanup(self) -> None:
        job_id = "leased-job"
        self.create_job(job_id, JobState.SUCCEEDED)
        with job_lease(self.jobs, job_id):
            report = self.reaper().prune_once(now=self.now)
            self.assertEqual(report.deleted, ())
            self.assertTrue(any(issue.phase == "process" for issue in report.deferred))

        recovered = self.reaper().prune_once(now=self.now)
        self.assertEqual(recovered.deleted, (job_id,))

    def test_partial_cleanup_recovers_after_restart(self) -> None:
        for phase in ("worktree", "artifacts"):
            with self.subTest(phase=phase):
                job_id = f"crash-{phase}"
                self.create_job(job_id, JobState.FAILED)

                def crash(current_phase, _manifest):
                    if current_phase == phase:
                        raise SimulatedCrash

                with self.assertRaises(SimulatedCrash):
                    self.reaper(phase_hook=crash).prune_once(now=self.now)
                self.assertTrue((self.jobs / f"{job_id}.json").exists())

                recovered = self.reaper().prune_once(now=self.now)
                self.assertEqual(recovered.deleted, (job_id,))
                self.assertFalse((self.jobs / job_id).exists())

    def test_mismatched_owned_branch_fails_closed(self) -> None:
        job_id = "wrong-branch"
        manifest, _ = self.store.create_or_get(JobRequest(job_id, "repo", "change"))
        resolver = RepositoryResolver(self.workspace, self.worktrees)
        prepared = resolver.prepare(resolver.resolve("repo"), job_id, "change")
        self.store.update(
            job_id,
            expected=(JobState.QUEUED,),
            transform=lambda item: item.evolve(
                state=JobState.FAILED,
                updated_at=self.old.isoformat(),
                branch="not-owned/by-this-job",
                worktree=str(prepared.path),
                repo_path=str(self.repository),
            ),
        )

        report = self.reaper().prune_once(now=self.now)

        self.assertEqual(report.deleted, ())
        self.assertTrue(prepared.path.exists())
        self.assertTrue((self.jobs / manifest.job_id).exists() is False)
        self.assertTrue((self.jobs / f"{manifest.job_id}.json").exists())

    def test_roots_must_be_resolved_and_disjoint(self) -> None:
        with self.assertRaises(RetentionSafetyError):
            validate_retention_roots(
                workspace=self.root,
                worktrees_root=self.root / "worktrees",
                jobs_root=self.root / "jobs",
            )

    def test_nonblocking_lease_reports_contention(self) -> None:
        with job_lease(self.jobs, "lease-only"):
            with self.assertRaises(JobLeaseBusy):
                with job_lease(self.jobs, "lease-only", blocking=False):
                    self.fail("contended lease unexpectedly acquired")

    def test_old_manifest_without_terminal_at_uses_updated_at(self) -> None:
        self.create_job("legacy-job", JobState.SUCCEEDED)
        path = self.jobs / "legacy-job.json"
        stored = json.loads(path.read_text(encoding="utf-8"))
        stored.pop("terminal_at")
        path.write_text(json.dumps(stored), encoding="utf-8")

        report = self.reaper().prune_once(now=self.now)

        self.assertEqual(report.deleted, ("legacy-job",))


if __name__ == "__main__":
    unittest.main()
