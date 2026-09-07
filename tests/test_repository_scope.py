from __future__ import annotations

import unittest

from app.repository_scope import (
    AMBIGUOUS_REPOSITORY,
    DOCUMENTATION_REPOSITORY_READ_ONLY,
    REPOSITORY_REQUIRED,
    UNKNOWN_REPOSITORY,
    execution_repositories,
    is_documentation_repository,
    mentioned_execution_repositories,
    resolve_execution_repository,
)


REPOSITORIES = (
    "brain-capnet",
    "capnet-next-lambda-tasks",
    "capnet-next-lambda-task-manager",
    "customer-service",
)


class RepositoryScopeTests(unittest.TestCase):
    def test_documentation_is_excluded_from_execution_inventory(self) -> None:
        for name in ("brain-capnet", "CAPNET-BRAIN", " Brain-Capnet "):
            self.assertTrue(is_documentation_repository(name))
        self.assertFalse(is_documentation_repository("customer-service"))
        self.assertEqual(
            execution_repositories((*REPOSITORIES, "capnet-brain", "CUSTOMER-SERVICE")),
            REPOSITORIES[1:],
        )

    def test_task_aliases_use_inventory_and_longest_overlapping_phrase(self) -> None:
        for alias in ("task", "tasks", "tarea", "tareas"):
            with self.subTest(alias=alias):
                self.assertEqual(
                    mentioned_execution_repositories(f"agrega campo en {alias}", REPOSITORIES),
                    ("capnet-next-lambda-tasks",),
                )
        self.assertEqual(
            mentioned_execution_repositories("edita task manager", REPOSITORIES),
            ("capnet-next-lambda-task-manager",),
        )
        self.assertEqual(mentioned_execution_repositories("edita tasks", ("customer-service",)), ())

    def test_identifiers_and_branch_names_do_not_become_aliases(self) -> None:
        for text in (
            "agrega task_available",
            "usa feature/tasks",
            "rama feature-task",
            "archivo tasks.py",
            "modelo tasks_response",
        ):
            with self.subTest(text=text):
                self.assertEqual(mentioned_execution_repositories(text, REPOSITORIES), ())

    def test_full_name_wins_over_generic_model_words(self) -> None:
        self.assertEqual(
            mentioned_execution_repositories("agrega tasks en customer-service", REPOSITORIES),
            ("customer-service",),
        )

    def test_two_full_names_remain_ambiguous_despite_different_lengths(self) -> None:
        self.assertEqual(
            resolve_execution_repository(
                "actualiza customer-service y capnet-next-lambda-tasks", None, REPOSITORIES
            ),
            (None, AMBIGUOUS_REPOSITORY),
        )

    def test_separate_aliases_are_ambiguous(self) -> None:
        self.assertEqual(
            resolve_execution_repository("actualiza tasks y task manager", None, REPOSITORIES),
            (None, AMBIGUOUS_REPOSITORY),
        )

    def test_explicit_unknown_repo_does_not_fall_back_to_memory(self) -> None:
        self.assertEqual(
            resolve_execution_repository(
                "agrega campo en repo capnet-next-lambda-typo", "customer-service", REPOSITORIES
            ),
            (None, UNKNOWN_REPOSITORY),
        )

    def test_brain_reference_and_execution_target_have_separate_roles(self) -> None:
        self.assertEqual(
            resolve_execution_repository(
                "lee brain-capnet y agrega campo en tasks", "brain-capnet", REPOSITORIES
            ),
            ("capnet-next-lambda-tasks", None),
        )
        self.assertEqual(
            resolve_execution_repository("edita brain-capnet", "customer-service", REPOSITORIES),
            (None, DOCUMENTATION_REPOSITORY_READ_ONLY),
        )
        self.assertEqual(
            resolve_execution_repository(
                "edita el repo brain-capnet usando customer-service", None, REPOSITORIES
            ),
            (None, DOCUMENTATION_REPOSITORY_READ_ONLY),
        )
        self.assertEqual(
            resolve_execution_repository(
                "agrega campo en tasks siguiendo la documentacion de brain-capnet", None, REPOSITORIES
            ),
            ("capnet-next-lambda-tasks", None),
        )
        self.assertEqual(
            resolve_execution_repository("agrega task_available", "brain-capnet", REPOSITORIES),
            (None, REPOSITORY_REQUIRED),
        )
