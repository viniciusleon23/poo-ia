from __future__ import annotations

import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
from typing import Sequence

from worker.config import WorkerSettings
from worker.github import GitHubPublisher, PublicationError
from worker.models import (
    DiffMeasurement,
    JobRequest,
    JobState,
    ValidationResult,
    ValidationStatus,
)
from worker.processes import CommandResult, CommandRunner
from worker.store import ManifestStore
from worker.validation import Validator


class FakePublicationRunner(CommandRunner):
    BASE_SHA = "a" * 40
    OWN_SHA = "b" * 40
    FOREIGN_SHA = "c" * 40
    PREPARED_PATCH = (
        "diff --git a/task.py b/task.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/task.py\n"
        "@@ -0,0 +1 @@\n"
        "+task_available = True\n"
    )

    def __init__(
        self,
        *,
        authenticated: bool = True,
        existing_url: str | None = None,
        remote_available: bool = True,
        push_available: bool = True,
        changed_patch: bool = False,
        validation_failure: bool = False,
        head_sha: str | None = None,
        head_parent: str | None = None,
        commit_message: str | None = None,
        remote_sha: str | None = None,
        mutate_head_during_validation: bool = False,
    ) -> None:
        self.authenticated = authenticated
        self.existing_url = existing_url
        self.remote_available = remote_available
        self.push_available = push_available
        self.changed_patch = changed_patch
        self.validation_failure = validation_failure
        self.head_sha = head_sha or self.BASE_SHA
        self.head_parent = head_parent
        self.commit_message = commit_message
        self.remote_sha = remote_sha
        self.dirty = self.head_sha == self.BASE_SHA
        self.mutate_head_during_validation = mutate_head_during_validation
        self.calls: list[tuple[str, ...]] = []

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
        command = tuple(str(part) for part in argv)
        self.calls.append(command)
        if command[:3] == ("fake-gh", "auth", "status"):
            return CommandResult(0 if self.authenticated else 1)
        if command[:3] == ("fake-gh", "pr", "view"):
            return (
                CommandResult(0, f"{self.existing_url}\n")
                if self.existing_url
                else CommandResult(1, "", "no pull requests found")
            )
        if command[:3] == ("fake-gh", "pr", "create"):
            self.existing_url = "https://github.com/example/repo/pull/42"
            return CommandResult(0, self.existing_url + "\n")
        if (
            len(command) >= 8
            and command[0] == "fake-git"
            and command[3:5] == ("worktree", "add")
        ):
            baseline = Path(command[-2])
            baseline.mkdir(parents=True, exist_ok=True)
            (baseline / "Makefile").write_text("test:\n\t@true\n", encoding="utf-8")
            return CommandResult(0)
        if command[:2] == ("fake-git", "ls-remote"):
            if not self.remote_available:
                return CommandResult(1)
            if self.remote_sha:
                return CommandResult(
                    0,
                    f"{self.remote_sha}\t{command[-1]}\n",
                )
            return CommandResult(0, "")
        if command[:3] == ("fake-git", "diff", "--numstat"):
            return CommandResult(0, "1\t0\ttask.py\n")
        if command[:3] == ("fake-git", "ls-files", "--others"):
            return CommandResult(0, "")
        if command[:3] == ("fake-git", "diff", "--binary"):
            patch = self.PREPARED_PATCH
            if self.changed_patch:
                patch = patch.replace("True", "False")
            digest = sha256(patch.encode()).hexdigest()
            if stdout_path is not None:
                stdout_path.parent.mkdir(parents=True, exist_ok=True)
                stdout_path.write_text(patch, encoding="utf-8")
            if len(command) == 4 and self.mutate_head_during_validation:
                self.head_sha = self.FOREIGN_SHA
                self.head_parent = self.BASE_SHA
                self.commit_message = "Commit created during validation"
                self.dirty = False
            return CommandResult(0, patch, "", stdout_sha256=digest)
        if command[:3] == ("fake-git", "status", "--porcelain=v1"):
            return CommandResult(0, " M task.py\n" if self.dirty else "")
        if command[:4] == ("fake-git", "diff", "--cached", "--quiet"):
            return CommandResult(1)
        if command[:3] == ("fake-git", "rev-parse", "--verify"):
            return CommandResult(0, self.head_sha + "\n")
        if command[:3] == ("fake-git", "rev-list", "--parents"):
            parent = self.head_parent or self.BASE_SHA
            return CommandResult(0, f"{self.head_sha} {parent}\n")
        if command[:3] == ("fake-git", "show", "-s"):
            return CommandResult(0, (self.commit_message or "") + "\n")
        if command[:2] == ("fake-git", "commit"):
            messages = [
                command[index + 1]
                for index, argument in enumerate(command[:-1])
                if argument == "-m"
            ]
            self.commit_message = "\n\n".join(messages)
            self.head_parent = self.head_sha
            self.head_sha = self.OWN_SHA
            self.dirty = False
            return CommandResult(0)
        if command[:2] == ("fake-git", "push"):
            if not self.push_available:
                return CommandResult(1, "", "non-fast-forward")
            self.remote_sha = command[-1].partition(":")[0]
            return CommandResult(0)
        if command == ("make", "test"):
            is_baseline = cwd is not None and cwd.name == "baseline"
            return CommandResult(
                1 if self.validation_failure and not is_baseline else 0
            )
        return CommandResult(0)


class GitHubPublisherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.worktree = root / "worktree"
        self.worktree.mkdir()
        self.settings = WorkerSettings(
            host="127.0.0.1",
            port=4097,
            username="test",
            password="long-enough-test-password",
            workspace=root / "workspace",
            worktrees_root=root / "worktrees",
            data_root=root / "data",
            git_executable="fake-git",
            gh_executable="fake-gh",
        )
        self.store = ManifestStore(self.settings.jobs_root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def prepare(
        self,
        job_id: str,
        *,
        validation: ValidationStatus = ValidationStatus.PASSED,
        over_budget: bool = False,
    ) -> None:
        self.store.create_or_get(JobRequest(job_id, "repo", "Add a field"))
        self.store.update(
            job_id,
            expected=(JobState.QUEUED,),
            transform=lambda manifest: manifest.evolve(
                state=JobState.PREPARED,
                worktree=str(self.worktree),
                branch=f"poo-ia/{job_id}-add-a-field",
                base_commit=FakePublicationRunner.BASE_SHA,
                base_branch="develop",
                repo_path=str(self.worktree),
                validation=ValidationResult(validation),
                diff=DiffMeasurement(1, 1, over_budget=over_budget),
                diff_sha256=sha256(
                    FakePublicationRunner.PREPARED_PATCH.encode()
                ).hexdigest(),
                summary="Added the requested field.",
            ),
        )

    def test_first_publish_commits_pushes_creates_one_pr_and_retry_is_idempotent(
        self,
    ) -> None:
        self.prepare("discord-901")
        commands = FakePublicationRunner()
        publisher = GitHubPublisher(self.settings, self.store, runner=commands)

        first = publisher.publish("discord-901")
        calls_after_first = list(commands.calls)
        second = publisher.publish("discord-901")

        self.assertEqual(first.state, JobState.SUCCEEDED)
        self.assertEqual(first.pr_url, "https://github.com/example/repo/pull/42")
        self.assertEqual(second.pr_url, first.pr_url)
        self.assertEqual(commands.calls, calls_after_first)
        self.assertEqual(
            sum(call[:3] == ("fake-gh", "pr", "create") for call in commands.calls),
            1,
        )
        self.assertTrue(
            any(call[:2] == ("fake-git", "push") for call in commands.calls)
        )
        self.assertTrue(
            any(call[:2] == ("fake-git", "ls-remote") for call in commands.calls)
        )
        create = next(
            call for call in commands.calls if call[:3] == ("fake-gh", "pr", "create")
        )
        self.assertEqual(create[create.index("--base") + 1], "develop")
        push = next(call for call in commands.calls if call[:2] == ("fake-git", "push"))
        self.assertEqual(
            push,
            (
                "fake-git",
                "push",
                "origin",
                FakePublicationRunner.OWN_SHA
                + ":refs/heads/poo-ia/discord-901-add-a-field",
            ),
        )
        commit = next(
            call for call in commands.calls if call[:2] == ("fake-git", "commit")
        )
        self.assertIn("Poo-IA-Job: discord-901", commit)

        verified_parent = next(
            call for call in commands.calls if call[:3] == ("fake-git", "rev-list", "--parents")
        )
        self.assertEqual(verified_parent[-1], FakePublicationRunner.OWN_SHA)
        self.assertTrue(
            any(call[:3] == ("fake-git", "show", "-s") for call in commands.calls)
        )

    def test_retry_adopts_existing_pr_only_after_owned_commit_and_remote_match(self) -> None:
        self.prepare("discord-902")
        commands = FakePublicationRunner(
            existing_url="https://github.com/example/repo/pull/9",
            head_sha=FakePublicationRunner.OWN_SHA,
            head_parent=FakePublicationRunner.BASE_SHA,
            commit_message="Poo-IA: Add a field\n\nPoo-IA-Job: discord-902",
            remote_sha=FakePublicationRunner.OWN_SHA,
        )
        result = GitHubPublisher(self.settings, self.store, runner=commands).publish(
            "discord-902"
        )

        self.assertEqual(result.pr_url, "https://github.com/example/repo/pull/9")
        self.assertTrue(any(call[0] == "fake-git" for call in commands.calls))
        self.assertTrue(
            any(call[:2] == ("fake-git", "ls-remote") for call in commands.calls)
        )
        self.assertFalse(
            any(call[:2] == ("fake-git", "commit") for call in commands.calls)
        )
        self.assertFalse(any(call[:2] == ("fake-git", "push") for call in commands.calls))
        self.assertFalse(
            any(call[:3] == ("fake-gh", "pr", "create") for call in commands.calls)
        )

    def test_rejects_commit_created_before_publisher_even_with_same_tree(self) -> None:
        self.prepare("discord-911")
        commands = FakePublicationRunner(
            head_sha=FakePublicationRunner.FOREIGN_SHA,
            head_parent=FakePublicationRunner.BASE_SHA,
            commit_message="Commit created by Codex",
        )

        with self.assertRaisesRegex(PublicationError, "not created by the publisher"):
            GitHubPublisher(self.settings, self.store, runner=commands).publish(
                "discord-911"
            )

        self.assertEqual(self.store.get("discord-911").state, JobState.PREPARED)
        self.assertFalse(any(call[:2] == ("fake-git", "push") for call in commands.calls))

    def test_rejects_owned_trailer_when_parent_is_not_exact_base(self) -> None:
        self.prepare("discord-912")
        commands = FakePublicationRunner(
            head_sha=FakePublicationRunner.FOREIGN_SHA,
            head_parent="d" * 40,
            commit_message="Poo-IA: Add a field\n\nPoo-IA-Job: discord-912",
        )

        with self.assertRaisesRegex(PublicationError, "parent is not the prepared base"):
            GitHubPublisher(self.settings, self.store, runner=commands).publish(
                "discord-912"
            )

        self.assertFalse(any(call[:2] == ("fake-git", "push") for call in commands.calls))

    def test_rejects_near_match_ownership_trailer(self) -> None:
        self.prepare("discord-916")
        commands = FakePublicationRunner(
            head_sha=FakePublicationRunner.OWN_SHA,
            head_parent=FakePublicationRunner.BASE_SHA,
            commit_message="Poo-IA: Add a field\n\nPoo-IA-Job: discord-916 ",
            remote_sha=FakePublicationRunner.OWN_SHA,
        )

        with self.assertRaisesRegex(PublicationError, "not created by the publisher"):
            GitHubPublisher(self.settings, self.store, runner=commands).publish(
                "discord-916"
            )

        self.assertFalse(any(call[:2] == ("fake-git", "push") for call in commands.calls))

    def test_rejects_owned_commit_when_fingerprint_does_not_match(self) -> None:
        self.prepare("discord-917")
        commands = FakePublicationRunner(
            head_sha=FakePublicationRunner.OWN_SHA,
            head_parent=FakePublicationRunner.BASE_SHA,
            commit_message="Poo-IA: Add a field\n\nPoo-IA-Job: discord-917",
            remote_sha=FakePublicationRunner.OWN_SHA,
            changed_patch=True,
        )

        with self.assertRaisesRegex(PublicationError, "changed during publication"):
            GitHubPublisher(self.settings, self.store, runner=commands).publish(
                "discord-917"
            )

        self.assertFalse(
            any(call[:3] == ("fake-gh", "pr", "view") for call in commands.calls)
        )

    def test_head_must_still_equal_base_immediately_before_commit(self) -> None:
        self.prepare("discord-913")
        commands = FakePublicationRunner(mutate_head_during_validation=True)

        with self.assertRaisesRegex(PublicationError, "HEAD changed before publication"):
            GitHubPublisher(self.settings, self.store, runner=commands).publish(
                "discord-913"
            )

        self.assertFalse(any(call[:2] == ("fake-git", "commit") for call in commands.calls))
        self.assertFalse(any(call[:2] == ("fake-git", "push") for call in commands.calls))

    def test_existing_pr_is_not_adopted_when_remote_sha_does_not_match(self) -> None:
        self.prepare("discord-914")
        commands = FakePublicationRunner(
            existing_url="https://github.com/example/repo/pull/99",
            head_sha=FakePublicationRunner.OWN_SHA,
            head_parent=FakePublicationRunner.BASE_SHA,
            commit_message="Poo-IA: Add a field\n\nPoo-IA-Job: discord-914",
            remote_sha=FakePublicationRunner.FOREIGN_SHA,
            push_available=False,
        )

        with self.assertRaisesRegex(PublicationError, "push the prepared branch"):
            GitHubPublisher(self.settings, self.store, runner=commands).publish(
                "discord-914"
            )

        self.assertFalse(
            any(call[:3] == ("fake-gh", "pr", "view") for call in commands.calls)
        )

    def test_existing_pr_does_not_bypass_gate(self) -> None:
        self.prepare("discord-915", over_budget=True)
        commands = FakePublicationRunner(
            existing_url="https://github.com/example/repo/pull/100",
            head_sha=FakePublicationRunner.OWN_SHA,
            head_parent=FakePublicationRunner.BASE_SHA,
            commit_message="Poo-IA: Add a field\n\nPoo-IA-Job: discord-915",
            remote_sha=FakePublicationRunner.OWN_SHA,
        )

        with self.assertRaisesRegex(PublicationError, "budget"):
            GitHubPublisher(self.settings, self.store, runner=commands).publish(
                "discord-915"
            )

        self.assertFalse(
            any(call[:3] == ("fake-gh", "pr", "view") for call in commands.calls)
        )

    def test_auth_failure_restores_prepared_state(self) -> None:
        self.prepare("discord-903")
        with self.assertRaisesRegex(PublicationError, "not authenticated"):
            GitHubPublisher(
                self.settings,
                self.store,
                runner=FakePublicationRunner(authenticated=False),
            ).publish("discord-903")
        self.assertEqual(self.store.get("discord-903").state, JobState.PREPARED)

    def test_remote_inspection_failure_happens_before_commit_or_push(self) -> None:
        self.prepare("discord-906")
        commands = FakePublicationRunner(remote_available=False)
        with self.assertRaisesRegex(PublicationError, "remote publication branch"):
            GitHubPublisher(self.settings, self.store, runner=commands).publish(
                "discord-906"
            )
        self.assertEqual(self.store.get("discord-906").state, JobState.PREPARED)
        self.assertFalse(
            any(
                call[:2] in {("fake-git", "commit"), ("fake-git", "push")}
                for call in commands.calls
            )
        )

    def test_over_budget_or_failed_validation_requires_explicit_override(self) -> None:
        self.prepare("discord-904", over_budget=True)
        with self.assertRaisesRegex(PublicationError, "budget"):
            GitHubPublisher(
                self.settings, self.store, runner=FakePublicationRunner()
            ).publish("discord-904")

        self.prepare("discord-905", validation=ValidationStatus.FAILED)
        with self.assertRaisesRegex(PublicationError, "override"):
            GitHubPublisher(
                self.settings, self.store, runner=FakePublicationRunner()
            ).publish("discord-905")

        result = GitHubPublisher(
            self.settings, self.store, runner=FakePublicationRunner()
        ).publish("discord-905", override=True)
        self.assertEqual(result.state, JobState.SUCCEEDED)

    def test_changed_worktree_is_blocked_before_commit_or_push(self) -> None:
        self.prepare("discord-907")
        commands = FakePublicationRunner(changed_patch=True)

        with self.assertRaisesRegex(PublicationError, "changed since it was prepared"):
            GitHubPublisher(self.settings, self.store, runner=commands).publish(
                "discord-907"
            )

        self.assertEqual(self.store.get("discord-907").state, JobState.PREPARED)
        self.assertFalse(
            any(
                call[:2] in {("fake-git", "commit"), ("fake-git", "push")}
                for call in commands.calls
            )
        )

    def test_fresh_validation_failure_blocks_publication(self) -> None:
        self.prepare("discord-908")
        (self.worktree / "Makefile").write_text("test:\n\t@false\n", encoding="utf-8")
        commands = FakePublicationRunner(validation_failure=True)
        validator = Validator(
            git_executable=self.settings.git_executable,
            runner=commands,
            timeout_seconds=self.settings.validation_timeout_seconds,
            enabled=True,
        )

        with self.assertRaisesRegex(PublicationError, "validation is failed"):
            GitHubPublisher(
                self.settings,
                self.store,
                runner=commands,
                validator=validator,
            ).publish("discord-908")

        self.assertFalse(
            any(call[:2] == ("fake-git", "push") for call in commands.calls)
        )

    def test_publish_claimed_continues_preclaimed_durable_job(self) -> None:
        self.prepare("discord-909")
        self.store.update(
            "discord-909",
            expected=(JobState.PREPARED,),
            transform=lambda item: item.evolve(
                state=JobState.PUBLISHING, process_pid=909
            ),
        )

        result = GitHubPublisher(
            self.settings, self.store, runner=FakePublicationRunner()
        ).publish_claimed("discord-909", process_pid=909)

        self.assertEqual(result.state, JobState.SUCCEEDED)

    def test_detached_base_cannot_be_published(self) -> None:
        self.prepare("discord-910")
        self.store.update(
            "discord-910",
            expected=(JobState.PREPARED,),
            transform=lambda item: item.evolve(base_branch="detached"),
        )

        with self.assertRaisesRegex(PublicationError, "detached HEAD"):
            GitHubPublisher(
                self.settings, self.store, runner=FakePublicationRunner()
            ).publish("discord-910")


if __name__ == "__main__":
    unittest.main()
