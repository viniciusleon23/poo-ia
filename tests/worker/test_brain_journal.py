from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.worker.test_repositories import git, initialize_repository
from worker.brain_journal import BrainJournal, BrainJournalError
from worker.config import WorkerSettings
from worker.models import (
    DiffMeasurement, JobManifest, JobRequest, JobState, ValidationResult, ValidationStatus,
)


class BrainJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.workspace = self.root / "workspace"
        self.brain = initialize_repository(self.workspace, "brain-capnet")
        self.worktrees = self.root / "worktrees"
        self.settings = WorkerSettings(
            host="127.0.0.1", port=4097, username="tests", password="test-password-value",
            workspace=self.workspace, worktrees_root=self.worktrees,
            data_root=self.root / "data",
        )
        self.journal = BrainJournal(self.settings)
        self.manifest = JobManifest.from_request(JobRequest(
            job_id="discord-123", repository="capnet-next-lambda-tasks",
            prompt="prompt-secret", preflight="preflight-secret", policy="policy-secret",
        )).evolve(
            state=JobState.PREPARED, branch="poo-ia/discord-123-add-field",
            base_commit="a" * 40, summary="summary-secret", error="error-secret",
            diff=DiffMeasurement(changed_files=1, changed_lines=4),
            validation=ValidationResult(
                status=ValidationStatus.PASSED, command=("echo", "command-secret"),
                exit_code=0, detail="validation-secret", log_path="log-secret",
            ),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_records_in_isolated_brain_worktree_without_changing_base_or_committing(self) -> None:
        head = git(self.brain, "rev-parse", "HEAD")
        branch = git(self.brain, "branch", "--show-current")

        result = self.journal.record(self.manifest)

        path = Path(result.path)
        self.assertEqual(result.state, "prepared")
        self.assertEqual(result.branch, "poo-ia/docs-discord-123")
        self.assertEqual(path, self.worktrees / "brain-docs-discord-123/Procesos/Poo-IA/discord-123.md")
        self.assertEqual(git(self.brain, "rev-parse", "HEAD"), head)
        self.assertEqual(git(self.brain, "branch", "--show-current"), branch)
        self.assertEqual(git(self.brain, "status", "--porcelain"), "")
        self.assertFalse((self.brain / "Procesos").exists())
        worktree = self.worktrees / "brain-docs-discord-123"
        self.assertEqual(git(worktree, "rev-parse", "HEAD"), head)
        self.assertIn("discord-123.md", git(worktree, "status", "--porcelain", "-uall"))

    def test_retry_is_idempotent_and_publication_updates_the_same_document(self) -> None:
        first = self.journal.record(self.manifest)
        original = Path(first.path).read_bytes()

        self.assertEqual(self.journal.record(self.manifest), first)
        self.assertEqual(Path(first.path).read_bytes(), original)
        published = self.manifest.evolve(
            state=JobState.SUCCEEDED, pr_url="https://github.com/capnet/tasks/pull/42",
        )
        final = self.journal.record(published)

        self.assertEqual(final, first)
        text = Path(final.path).read_text(encoding="utf-8")
        self.assertIn("https://github.com/capnet/tasks/pull/42", text)
        self.assertIn("succeeded", text)
        self.assertEqual(len(tuple(Path(final.path).parent.glob("*.md"))), 1)

    def test_document_contains_only_structured_execution_metadata(self) -> None:
        record = self.journal.record(self.manifest)
        text = Path(record.path).read_text(encoding="utf-8")

        for value in (
            "prompt-secret", "preflight-secret", "policy-secret", "summary-secret",
            "error-secret", "command-secret", "validation-secret", "log-secret",
        ):
            self.assertNotIn(value, text)
        for expected in ("capnet-next-lambda-tasks", "poo-ia/discord-123-add-field", "passed", "a" * 40):
            self.assertIn(expected, text)

    def test_missing_brain_does_not_create_a_fake_repository(self) -> None:
        settings = WorkerSettings(
            host="127.0.0.1", port=4097, username="tests", password="test-password-value",
            workspace=self.root / "missing", worktrees_root=self.worktrees,
        )
        with self.assertRaises(BrainJournalError):
            BrainJournal(settings).record(self.manifest)
        self.assertFalse((settings.workspace / "brain-capnet").exists())

    def test_rejects_worktree_from_another_repository_even_with_matching_branch(self) -> None:
        other = initialize_repository(self.root / "other", "unrelated")
        self.worktrees.mkdir()
        target = self.worktrees / "brain-docs-discord-123"
        git(other, "worktree", "add", "-b", "poo-ia/docs-discord-123", str(target))

        with self.assertRaisesRegex(BrainJournalError, "worktree"):
            self.journal.record(self.manifest)
        self.assertFalse((target / "Procesos").exists())

    def test_rejects_existing_foreign_branch_or_unowned_document(self) -> None:
        self.worktrees.mkdir()
        target = self.worktrees / "brain-docs-discord-123"
        git(self.brain, "worktree", "add", "-b", "someone-else", str(target))
        with self.assertRaisesRegex(BrainJournalError, "worktree"):
            self.journal.record(self.manifest)
        git(target, "branch", "-m", "poo-ia/docs-discord-123")
        path = target / "Procesos/Poo-IA/discord-123.md"
        path.parent.mkdir(parents=True)
        path.write_text("Manual notes; do not overwrite.\n", encoding="utf-8")

        with self.assertRaisesRegex(BrainJournalError, "documento"):
            self.journal.record(self.manifest)
        self.assertEqual(path.read_text(), "Manual notes; do not overwrite.\n")

    def test_rejects_symlinked_worktree_and_document_ancestors(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        self.worktrees.mkdir()
        target = self.worktrees / "brain-docs-discord-123"
        target.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(BrainJournalError):
            self.journal.record(self.manifest)
        target.unlink()
        record = self.journal.record(self.manifest)
        path = Path(record.path)
        path.unlink()
        path.parent.rmdir()
        path.parent.symlink_to(outside, target_is_directory=True)

        with self.assertRaises(BrainJournalError):
            self.journal.record(self.manifest)
        self.assertEqual(tuple(outside.iterdir()), ())

    def test_rejects_document_symlink_and_unsafe_ids(self) -> None:
        record = self.journal.record(self.manifest)
        path = Path(record.path)
        outside = self.root / "outside.md"
        outside.write_text("keep", encoding="utf-8")
        path.unlink()
        path.symlink_to(outside)
        with self.assertRaises(BrainJournalError):
            self.journal.record(self.manifest)
        self.assertEqual(outside.read_text(), "keep")
        with self.assertRaises(BrainJournalError):
            self.journal.record(self.manifest.evolve(job_id="../../escape"))
