from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from worker.repositories import RepositoryError, RepositoryResolver


def git(cwd: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", *arguments), cwd=cwd, text=True, capture_output=True, check=True
    )
    return completed.stdout.strip()


def initialize_repository(path: Path, name: str = "repo") -> Path:
    repository = path / name
    repository.mkdir(parents=True)
    git(repository, "init", "-q")
    git(repository, "config", "user.email", "tests@example.invalid")
    git(repository, "config", "user.name", "Worker Tests")
    (repository / "README.md").write_text("base\n", encoding="utf-8")
    git(repository, "add", "README.md")
    git(repository, "commit", "-qm", "base")
    return repository


class RepositoryResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.repository = initialize_repository(self.workspace, "Capnet-Service")
        self.worktrees = self.root / "worktrees"
        self.resolver = RepositoryResolver(self.workspace, self.worktrees)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_resolves_case_insensitive_alias_and_rejects_traversal(self) -> None:
        snapshot = self.resolver.resolve("capnet-service")
        self.assertEqual(snapshot.path, self.repository.resolve())

        with self.assertRaisesRegex(RepositoryError, "direct workspace"):
            self.resolver.resolve("../Capnet-Service")

    def test_rejects_dirty_base_checkout(self) -> None:
        (self.repository / "README.md").write_text("dirty\n", encoding="utf-8")
        self.assertEqual(self.resolver.clean_names(), ())
        with self.assertRaisesRegex(RepositoryError, "local changes"):
            self.resolver.resolve("Capnet-Service")

    def test_prepares_deterministic_isolated_worktree(self) -> None:
        base_branch = git(self.repository, "branch", "--show-current")
        base_commit = git(self.repository, "rev-parse", "HEAD")
        snapshot = self.resolver.resolve("Capnet-Service")

        first = self.resolver.prepare(snapshot, "discord-456", "Add task available")
        second = self.resolver.prepare(snapshot, "discord-456", "Add task available")

        self.assertEqual(first, second)
        self.assertEqual(first.path, (self.worktrees / "discord-456").resolve())
        self.assertEqual(first.branch, "poo-ia/discord-456-add-task-available")
        self.assertEqual(git(self.repository, "branch", "--show-current"), base_branch)
        self.assertEqual(git(self.repository, "rev-parse", "HEAD"), base_commit)

    def test_rejects_ambiguous_normalized_alias(self) -> None:
        initialize_repository(self.workspace, "capnet_service")
        with self.assertRaisesRegex(RepositoryError, "ambiguous"):
            self.resolver.resolve("capnetservice")

    def test_resume_keeps_the_existing_worktree_base_when_checkout_advances(self) -> None:
        original = self.resolver.resolve("Capnet-Service")
        prepared = self.resolver.prepare(original, "discord-457", "Small change")
        (self.repository / "README.md").write_text("new base\n", encoding="utf-8")
        git(self.repository, "add", "README.md")
        git(self.repository, "commit", "-qm", "advance base")
        advanced = self.resolver.resolve("Capnet-Service")

        resumed = self.resolver.prepare(advanced, "discord-457", "Small change")

        self.assertEqual(resumed.path, prepared.path)
        self.assertEqual(resumed.repository.base_commit, original.base_commit)
        self.assertNotEqual(resumed.repository.base_commit, advanced.base_commit)

    def test_refuses_to_resume_a_dirty_existing_worktree(self) -> None:
        snapshot = self.resolver.resolve("Capnet-Service")
        prepared = self.resolver.prepare(snapshot, "discord-458", "Small change")
        (prepared.path / "partial.txt").write_text("unconfirmed\n", encoding="utf-8")

        with self.assertRaisesRegex(RepositoryError, "unconfirmed"):
            self.resolver.prepare(snapshot, "discord-458", "Small change")

        self.assertFalse(
            self.resolver.interrupted_retry_is_safe(
                job_id="discord-458",
                worktree=str(prepared.path),
                branch=prepared.branch,
                base_commit=snapshot.base_commit,
            )
        )


if __name__ == "__main__":
    unittest.main()
