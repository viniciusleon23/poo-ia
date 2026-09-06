from __future__ import annotations

import unittest

from app.models import Backend, Intent, JobStatus, RouteDecision


class DomainModelTests(unittest.TestCase):
    def test_route_decision_is_transport_neutral_and_immutable(self) -> None:
        decision = RouteDecision(
            intent=Intent.CODE_CHANGE,
            backend=Backend.WORKER,
            repository="capnet-next-lambda-tasks",
            reason="explicit mutable request",
        )

        self.assertEqual(decision.intent.value, "code_change")
        self.assertEqual(decision.backend.value, "worker")
        with self.assertRaises((AttributeError, TypeError)):
            decision.repository = "other"  # type: ignore[misc]

    def test_only_finished_job_states_are_terminal(self) -> None:
        terminal = {state for state in JobStatus if state.is_terminal}
        self.assertEqual(
            terminal,
            {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED},
        )
