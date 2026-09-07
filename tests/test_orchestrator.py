from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.memory import MemoryStore
from app.models import (
    ConversationKey,
    InboundMessage,
    Intent,
    JobKind,
    JobStatus,
    RequestStatus,
)
from app.orchestrator import (
    JOB_NOT_FOUND_MESSAGE,
    PROCESSING_FAILED_MESSAGE,
    PooIAOrchestrator,
)
from app.outbox import DurableOutbox
from app.storage import SQLiteStorage
from app.worker_client import WorkerNotFoundError


KEY = ConversationKey("discord", 100, 200)


class FakeOllama:
    def __init__(self, response: str = "respuesta local") -> None:
        self.response = response
        self.prompts: list[str] = []

    async def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.response


class FakeOpenCode:
    def __init__(self, response: str = "resultado documental con ruta/file.py") -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.preflight_response: str | None = None
        self.active_session_id: str | None = None
        self.maximum_active = 0
        self._active = 0
        self.block = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.abort_calls = 0
        self.cleanup_calls: list[str] = []
        self.cleanup_error: Exception | None = None
        self.cleanup_block = False
        self.cleanup_started = asyncio.Event()
        self.cleanup_release = asyncio.Event()
        self.research_error_after_session: Exception | None = None

    async def research(self, prompt: str, **kwargs: object) -> str:
        self.calls.append((prompt, dict(kwargs)))
        self._active += 1
        self.maximum_active = max(self.maximum_active, self._active)
        self.active_session_id = f"session-{len(self.calls)}"
        callback = kwargs.get("on_session_created")
        if callable(callback):
            callback_result = callback(self.active_session_id)
            if asyncio.iscoroutine(callback_result):
                await callback_result
        self.started.set()
        try:
            if self.block:
                await self.release.wait()
            if self.research_error_after_session is not None:
                raise self.research_error_after_session
            if prompt.startswith("Prepara un preflight"):
                return self.preflight_response or json.dumps({
                    "repository": kwargs.get("active_repository"),
                    "status": "ready",
                    "files": ["schemas/base_response.py"],
                    "notes": "Archivo de tareas verificado.",
                    "missing_information": [],
                })
            return self.response
        finally:
            self._active -= 1
            self.active_session_id = None

    async def abort_active(self) -> str | None:
        self.abort_calls += 1
        return self.active_session_id

    async def cleanup_session(self, session_id: str) -> None:
        self.cleanup_calls.append(session_id)
        self.cleanup_started.set()
        if self.cleanup_block:
            await self.cleanup_release.wait()
        if self.cleanup_error is not None:
            raise self.cleanup_error


class FakeWorker:
    def __init__(self) -> None:
        self.aws_calls: list[tuple[str, str | None, str | None]] = []
        self.create_calls: list[dict[str, object]] = []
        self.get_calls: list[str] = []
        self.cancel_calls: list[str] = []
        self.publish_calls: list[tuple[str, bool]] = []
        self.polls: dict[str, list[dict[str, object]]] = {}
        self.cancel_result: dict[str, object] | None = None

    async def query_aws(self, action: str, *, table: str | None = None, log_group: str | None = None):
        self.aws_calls.append((action, table, log_group))
        return {"state": "succeeded", "message": "Tabla Tasks: ACTIVE (metadatos)."}

    async def create_codex_job(self, **kwargs: object):
        self.create_calls.append(dict(kwargs))
        job_id = str(kwargs["job_id"])
        prepared = {
            "job_id": job_id,
            "state": "prepared",
            "repository": kwargs["repository"],
            "branch": f"poo-ia/{job_id[:8]}-change",
            "summary": "campo agregado",
            "validation": {"status": "passed"},
            "diff": {"changed_files": 1, "changed_lines": 2},
        }
        sequence = [
                {"job_id": job_id, "state": "running"},
                prepared,
        ]
        if kwargs.get("publish"):
            sequence.append(
                prepared
                | {
                    "state": "succeeded",
                    "pr_url": "https://github.com/example/repo/pull/8",
                }
            )
        self.polls.setdefault(job_id, sequence)
        return {"job_id": job_id, "state": "queued"}

    async def get_job(self, job_id: str):
        self.get_calls.append(job_id)
        if job_id not in self.polls:
            raise WorkerNotFoundError(f"worker job {job_id} was not found")
        sequence = self.polls[job_id]
        if len(sequence) > 1:
            return sequence.pop(0)
        return sequence[0]

    async def cancel_job(self, job_id: str):
        self.cancel_calls.append(job_id)
        return self.cancel_result or {"job_id": job_id, "state": "cancelled"}

    async def publish_job(self, job_id: str, *, override: bool = False):
        self.publish_calls.append((job_id, override))
        manifest = {
            "job_id": job_id,
            "state": "succeeded",
            "repository": "capnet-next-lambda-tasks",
            "branch": f"poo-ia/{job_id[:8]}-change",
            "summary": "campo agregado y publicado",
            "validation": {"status": "passed"},
            "pr_url": "https://github.com/example/repo/pull/7",
        }
        self.polls[job_id] = [manifest]
        return manifest


class OrchestratorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary.name) / "state.sqlite3"
        self.storage = SQLiteStorage(self.database_path)
        self.memory = MemoryStore(self.storage)
        self.outbox = DurableOutbox(self.storage)
        self.ollama = FakeOllama()
        self.opencode = FakeOpenCode()
        self.worker = FakeWorker()
        self.orchestrator = PooIAOrchestrator(
            storage=self.storage,
            memory=self.memory,
            outbox=self.outbox,
            ollama=self.ollama,
            content_root=Path(self.temporary.name),
            opencode=self.opencode,
            worker=self.worker,
            repositories=(
                "brain-capnet",
                "capnet-next-lambda-tasks",
                "customer-service",
            ),
            worker_poll_seconds=0.001,
            clock=lambda: 1_000,
        )

    async def asyncTearDown(self) -> None:
        await self.orchestrator.close()
        self.storage.close()
        self.temporary.cleanup()

    async def flush_all(self) -> list[str]:
        sent: list[str] = []

        async def sender(part):
            sent.append(part.content)
            return SimpleNamespace(id=f"discord-{len(sent)}")

        await self.orchestrator.flush_outputs(sender)
        return sent

    async def test_chat_runs_directly_and_enters_memory_only_after_discord_ack(self) -> None:
        submission = await self.orchestrator.handle(
            InboundMessage(1, 100, 200, "hola")
        )

        self.assertEqual(submission.intent, Intent.CHAT)
        self.assertFalse(submission.queued)
        self.assertEqual(len(self.ollama.prompts), 1)
        self.assertEqual(self.memory.snapshot(KEY).exchanges, ())
        sent = await self.flush_all()
        self.assertEqual(sent, ["respuesta local"])
        snapshot = self.memory.snapshot(KEY)
        self.assertEqual(snapshot.exchanges[0].user_text, "hola")
        self.assertEqual(snapshot.exchanges[0].assistant_text, "respuesta local")

    async def test_start_recovers_direct_chat_registered_before_crash(self) -> None:
        inbound = InboundMessage(75, 100, 200, "hola tras reinicio")
        request = self.storage.register_inbound(
            inbound,
            intent=Intent.CHAT,
            backend="ollama",
        ).request

        await self.orchestrator.start()

        self.assertEqual(len(self.ollama.prompts), 1)
        self.assertEqual(
            [part.content for part in self.orchestrator.pending_outputs(KEY)],
            ["respuesta local"],
        )
        self.assertEqual(
            self.storage.get_inbound(request.request_id).status,
            RequestStatus.PROCESSING,
        )

    async def test_duplicate_event_retries_orphan_direct_request_in_live_process(self) -> None:
        await self.orchestrator.start()
        inbound = InboundMessage(76, 100, 200, "hola huérfano")
        request = self.storage.register_inbound(
            inbound,
            intent=Intent.CHAT,
            backend="ollama",
        ).request

        duplicate = await self.orchestrator.handle(inbound)

        self.assertFalse(duplicate.created)
        self.assertEqual(len(self.ollama.prompts), 1)
        self.assertTrue(self.storage.request_has_outbox(request.request_id))

    async def test_start_does_not_regenerate_direct_request_with_existing_outbox(self) -> None:
        inbound = InboundMessage(77, 100, 200, "hola ya contestado")
        request = self.storage.register_inbound(
            inbound,
            intent=Intent.CHAT,
            backend="ollama",
        ).request
        self.outbox.enqueue(
            request.request_id,
            "respuesta persistida",
            backend="ollama",
            remember_exchange=True,
        )

        await self.orchestrator.start()

        self.assertEqual(self.ollama.prompts, [])
        self.assertEqual(
            [part.content for part in self.orchestrator.pending_outputs(KEY)],
            ["respuesta persistida"],
        )

    async def test_adapter_failure_is_durable_idempotent_and_not_remembered(self) -> None:
        inbound = InboundMessage(66, 100, 200, "mensaje que falló")

        self.assertTrue(await self.orchestrator.record_processing_failure(inbound))
        self.assertFalse(await self.orchestrator.record_processing_failure(inbound))

        request = self.storage.get_inbound(
            self.storage.register_inbound(inbound).request.request_id
        )
        self.assertIsNotNone(request)
        self.assertEqual(request.status, RequestStatus.FAILED)
        self.assertEqual(
            [item.content for item in self.orchestrator.pending_outputs(KEY)],
            [PROCESSING_FAILED_MESSAGE],
        )
        self.assertEqual(await self.flush_all(), [PROCESSING_FAILED_MESSAGE])
        self.assertEqual(self.memory.snapshot(KEY).exchanges, ())

    async def test_research_is_queued_and_result_uses_completed_context(self) -> None:
        await self.orchestrator.handle(InboundMessage(2, 100, 200, "hola"))
        await self.flush_all()
        submission = await self.orchestrator.handle(
            InboundMessage(3, 100, 200, "¿Dónde se define task_available?")
        )

        self.assertTrue(submission.queued)
        self.assertEqual(submission.intent, Intent.RESEARCH)
        await self.orchestrator.scheduler.wait_idle(timeout=2)
        sent = await self.flush_all()

        self.assertTrue(any("en cola" in text for text in sent))
        self.assertTrue(any("resultado documental" in text for text in sent))
        research_call = self.opencode.calls[-1]
        self.assertIn("hola", str(research_call[1]["conversation_context"]))
        self.assertEqual(self.storage.get_job(submission.job_id).state, JobStatus.SUCCEEDED)
        self.assertEqual(len(self.memory.snapshot(KEY).exchanges), 2)

    async def test_recovered_research_aborts_checkpointed_remote_session_first(self) -> None:
        registration = self.storage.register_inbound(
            InboundMessage(71, 100, 200, "investiga task_available"),
            job_kind=JobKind.RESEARCH,
            payload={"question": "investiga task_available"},
        )
        job = registration.job
        self.storage.transition_job(
            job.job_id,
            JobStatus.RUNNING,
            checkpoint={"opencode_session_id": "orphan-session"},
        )

        await self.orchestrator.start()
        await self.orchestrator.scheduler.wait_idle(timeout=2)

        self.assertEqual(self.opencode.cleanup_calls, ["orphan-session"])
        recovered = self.storage.get_job(job.job_id)
        self.assertEqual(recovered.state, JobStatus.SUCCEEDED)
        self.assertEqual(recovered.checkpoint, {})

    async def test_recovered_research_keeps_checkpoint_and_does_not_replace_failed_cleanup(self) -> None:
        registration = self.storage.register_inbound(
            InboundMessage(74, 100, 200, "investiga task_available"),
            job_kind=JobKind.RESEARCH,
            payload={"question": "investiga task_available"},
        )
        job = registration.job
        assert job is not None
        self.storage.transition_job(
            job.job_id,
            JobStatus.RUNNING,
            checkpoint={"opencode_session_id": "orphan-session"},
        )
        self.opencode.cleanup_error = RuntimeError("cleanup unavailable")

        outcome_error: Exception | None = None
        try:
            await self.orchestrator._execute_heavy_job(self.storage.get_job(job.job_id))
        except Exception as error:
            outcome_error = error

        self.assertIsNotNone(outcome_error)
        self.assertEqual(self.opencode.cleanup_calls, ["orphan-session"])
        self.assertEqual(self.opencode.calls, [])
        self.assertEqual(
            self.storage.get_job(job.job_id).checkpoint,
            {"opencode_session_id": "orphan-session"},
        )

    async def test_terminal_research_retries_orphan_cleanup_after_restart(self) -> None:
        registration = self.storage.register_inbound(
            InboundMessage(78, 100, 200, "investiga task_available"),
            job_kind=JobKind.RESEARCH,
            payload={"question": "investiga task_available"},
        )
        job = registration.job
        assert job is not None
        self.storage.transition_job(
            job.job_id,
            JobStatus.RUNNING,
            checkpoint={"opencode_session_id": "orphan-session"},
        )
        self.opencode.cleanup_error = RuntimeError("cleanup unavailable")

        await self.orchestrator.start()
        await self.orchestrator.scheduler.wait_idle(timeout=2)
        failed = self.storage.get_job(job.job_id)
        self.assertEqual(failed.state, JobStatus.FAILED)
        self.assertEqual(
            failed.checkpoint, {"opencode_session_id": "orphan-session"}
        )
        await self.orchestrator.close()

        self.opencode.cleanup_error = None
        restarted = PooIAOrchestrator(
            storage=self.storage,
            memory=self.memory,
            outbox=self.outbox,
            ollama=self.ollama,
            content_root=Path(self.temporary.name),
            opencode=self.opencode,
            worker=self.worker,
            repositories=("capnet-next-lambda-tasks",),
            worker_poll_seconds=0.001,
            clock=lambda: 1_000,
        )
        try:
            await restarted.start()
            cleanup = restarted._terminal_session_cleanup
            assert cleanup is not None
            await asyncio.wait_for(cleanup, timeout=0.1)
            cleaned = self.storage.get_job(job.job_id)
            self.assertEqual(
                self.opencode.cleanup_calls,
                ["orphan-session", "orphan-session", "orphan-session"],
            )
            self.assertEqual(cleaned.state, JobStatus.FAILED)
            self.assertEqual(cleaned.checkpoint, {})
        finally:
            await restarted.close()

    async def test_terminal_orphan_cleanup_never_blocks_discord_startup(self) -> None:
        registration = self.storage.register_inbound(
            InboundMessage(79, 100, 200, "investiga task_available"),
            job_kind=JobKind.RESEARCH,
            payload={"question": "investiga task_available"},
        )
        job = registration.job
        assert job is not None
        self.storage.transition_job(job.job_id, JobStatus.RUNNING)
        self.storage.update_running_job_checkpoint(
            job.job_id, {"opencode_session_id": "slow-orphan"}
        )
        self.storage.transition_job(job.job_id, JobStatus.FAILED)
        self.opencode.cleanup_block = True

        await asyncio.wait_for(self.orchestrator.start(), timeout=0.1)
        await asyncio.wait_for(self.opencode.cleanup_started.wait(), timeout=0.1)

        self.assertEqual(
            self.storage.get_job(job.job_id).checkpoint,
            {"opencode_session_id": "slow-orphan"},
        )
        self.opencode.cleanup_release.set()
        cleanup = self.orchestrator._terminal_session_cleanup
        assert cleanup is not None
        await asyncio.wait_for(cleanup, timeout=0.1)
        self.assertEqual(self.storage.get_job(job.job_id).checkpoint, {})

    async def test_nonterminal_orphan_cleanup_uses_short_recovery_timeout(self) -> None:
        registration = self.storage.register_inbound(
            InboundMessage(80, 100, 200, "investiga task_available"),
            job_kind=JobKind.RESEARCH,
            payload={"question": "investiga task_available"},
        )
        job = registration.job
        assert job is not None
        self.storage.transition_job(
            job.job_id,
            JobStatus.RUNNING,
            checkpoint={"opencode_session_id": "slow-running-orphan"},
        )
        self.opencode.cleanup_block = True

        with patch(
            "app.orchestrator.OPENCODE_RECOVERY_CLEANUP_TIMEOUT_SECONDS", 0.01
        ):
            await asyncio.wait_for(self.orchestrator.start(), timeout=0.1)

        self.assertGreaterEqual(
            self.opencode.cleanup_calls.count("slow-running-orphan"), 1
        )

    async def test_live_research_failure_schedules_its_session_cleanup(self) -> None:
        await self.orchestrator.start()
        startup_cleanup = self.orchestrator._terminal_session_cleanup
        assert startup_cleanup is not None
        await asyncio.wait_for(startup_cleanup, timeout=0.1)
        self.opencode.research_error_after_session = RuntimeError(
            "final session delete failed"
        )

        submission = await self.orchestrator.handle(
            InboundMessage(81, 100, 200, "investiga task_available")
        )
        await self.orchestrator.scheduler.wait_idle(timeout=2)
        cleanup = self.orchestrator._terminal_session_cleanup
        assert cleanup is not None
        await asyncio.wait_for(cleanup, timeout=0.1)

        failed = self.storage.get_job(submission.job_id)
        self.assertEqual(failed.state, JobStatus.FAILED)
        self.assertEqual(self.opencode.cleanup_calls, ["session-1"])
        self.assertEqual(failed.checkpoint, {})

    async def test_running_research_checkpoints_remote_session_before_result(self) -> None:
        self.opencode.block = True
        submission = await self.orchestrator.handle(
            InboundMessage(72, 100, 200, "investiga el servicio de tareas")
        )
        await asyncio.wait_for(self.opencode.started.wait(), timeout=1)

        running = self.storage.get_job(submission.job_id)
        self.assertEqual(
            running.checkpoint,
            {"opencode_session_id": "session-1"},
        )

        self.opencode.release.set()
        await self.orchestrator.scheduler.wait_idle(timeout=2)
        self.assertEqual(self.storage.get_job(submission.job_id).checkpoint, {})

    async def test_research_citation_sets_one_service_repo_and_followup_survives_reopen(self) -> None:
        self.opencode.response = (
            "La documentación en brain-capnet/ai/tasks.md apunta a "
            "capnet-next-lambda-tasks/schemas/base_response.py."
        )
        research = await self.orchestrator.handle(
            InboundMessage(31, 100, 200, "investiga el campo task_available")
        )
        await self.orchestrator.scheduler.wait_idle(timeout=2)

        conversation = self.storage.get_conversation(KEY)
        self.assertEqual(conversation.active_repository, "capnet-next-lambda-tasks")
        reopened = SQLiteStorage(self.database_path)
        self.assertEqual(
            reopened.get_conversation(KEY).active_repository,
            "capnet-next-lambda-tasks",
        )
        reopened.close()

        follow_up = await self.orchestrator.handle(
            InboundMessage(32, 100, 200, "hazlo")
        )
        await self.orchestrator.scheduler.wait_idle(timeout=2)

        self.assertEqual(self.storage.get_job(research.job_id).state, JobStatus.SUCCEEDED)
        self.assertEqual(follow_up.intent, Intent.CODE_CHANGE)
        self.assertEqual(
            self.worker.create_calls[-1]["repository"],
            "capnet-next-lambda-tasks",
        )

    async def test_brain_research_never_becomes_execution_context(self) -> None:
        self.opencode.response = "La guía está en brain-capnet/ai/rutas-de-consulta.md"
        await self.orchestrator.handle(InboundMessage(120, 100, 200, "investiga la guía documental"))
        await self.orchestrator.scheduler.wait_idle(timeout=2)
        self.assertIsNone(self.storage.get_conversation(KEY).active_repository)
        submission = await self.orchestrator.handle(InboundMessage(
            121, 100, 200,
            "Hola necesito agregar este campo task_available de tipo boleando en task, y que todo sea por defecto como true",
        ))
        await self.orchestrator.scheduler.wait_idle(timeout=2)
        self.assertEqual(self.storage.get_job(submission.job_id).state, JobStatus.PREPARED)
        self.assertEqual(self.worker.create_calls[-1]["repository"], "capnet-next-lambda-tasks")

    async def test_preflight_repository_mismatch_never_reaches_worker(self) -> None:
        self.opencode.preflight_response = json.dumps({
            "repository": "brain-capnet", "status": "ready", "files": ["README.md"],
            "notes": "La evidencia contradice el destino", "missing_information": [],
        })
        submission = await self.orchestrator.handle(InboundMessage(
            122, 100, 200, "agrega un campo en capnet-next-lambda-tasks",
        ))
        await self.orchestrator.scheduler.wait_idle(timeout=2)
        self.assertEqual(self.worker.create_calls, [])
        self.assertEqual(self.storage.get_job(submission.job_id).state, JobStatus.FAILED)

    async def test_dependency_citation_cannot_replace_explicit_execution_target(self) -> None:
        self.opencode.response = "Depende del código customer-service/models.py."
        await self.orchestrator.handle(InboundMessage(127, 100, 200, "investiga tasks"))
        await self.orchestrator.scheduler.wait_idle(timeout=2)
        self.assertEqual(self.storage.get_conversation(KEY).active_repository, "capnet-next-lambda-tasks")
        await self.orchestrator.handle(InboundMessage(128, 100, 200, "agrega el campo example sin investigar"))
        await self.orchestrator.scheduler.wait_idle(timeout=2)
        self.assertEqual(self.worker.create_calls[-1]["repository"], "capnet-next-lambda-tasks")

    async def test_capabilities_are_answered_by_core_without_read_only_research(self) -> None:
        submission = await self.orchestrator.handle(InboundMessage(123, 100, 200, "Ya puedes editar?"))
        self.assertIsNone(submission.job_id)
        self.assertEqual(self.opencode.calls, [])
        sent = " ".join(await self.flush_all())
        self.assertIn("repositorio de ejecución", sent)
        self.assertIn("brain", sent)

    async def test_failed_job_notification_includes_actual_codex_explanation(self) -> None:
        registration = self.storage.register_inbound(
            InboundMessage(124, 100, 200, "agrega campo"), job_kind=JobKind.CODEX,
            repository="capnet-next-lambda-tasks", payload={},
        )
        self.storage.transition_job(registration.job.job_id, JobStatus.RUNNING)
        failed = self.storage.transition_job(
            registration.job.job_id, JobStatus.FAILED,
            summary="Falta el archivo schemas/base_response.py en este worktree.",
            safe_error="Codex completed but produced no repository changes",
        )
        await self.orchestrator._on_job_finished(failed)
        sent = " ".join(await self.flush_all())
        self.assertIn("Falta el archivo", sent)
        self.assertIn("capnet-next-lambda-tasks", sent)

    async def test_pr_for_other_explicit_repository_never_publishes_active_job(self) -> None:
        registration = self.storage.register_inbound(
            InboundMessage(125, 100, 200, "agrega campo"), job_kind=JobKind.CODEX,
            repository="capnet-next-lambda-tasks", payload={},
        )
        self.storage.transition_job(registration.job.job_id, JobStatus.RUNNING)
        self.storage.transition_job(registration.job.job_id, JobStatus.PREPARED)
        submission = await self.orchestrator.handle(InboundMessage(
            126, 100, 200, f"arma el PR del trabajo {registration.job.job_id} en customer-service",
        ))
        self.assertIsNone(submission.job_id)
        self.assertEqual(self.worker.publish_calls, [])
        self.assertIn("otro repositorio", " ".join(await self.flush_all()))

    async def test_combined_change_and_pr_after_research_starts_new_codex_job(self) -> None:
        self.opencode.response = (
            "La evidencia está en "
            "capnet-next-lambda-tasks/schemas/base_response.py."
        )
        research = await self.orchestrator.handle(
            InboundMessage(67, 100, 200, "revisa dónde agregar task_available")
        )
        await self.orchestrator.scheduler.wait_idle(timeout=2)
        await self.flush_all()

        combined = await self.orchestrator.handle(
            InboundMessage(68, 100, 200, "Hecho, agrégalo y arma el PR")
        )
        await self.orchestrator.scheduler.wait_idle(timeout=2)

        self.assertNotEqual(combined.job_id, research.job_id)
        self.assertEqual(self.storage.get_job(combined.job_id).kind, JobKind.CODEX)
        self.assertEqual(
            self.worker.create_calls[-1]["repository"],
            "capnet-next-lambda-tasks",
        )
        self.assertTrue(self.worker.create_calls[-1]["publish"])

    async def test_plain_pr_after_research_does_not_invent_a_change(self) -> None:
        self.opencode.response = (
            "Ver capnet-next-lambda-tasks/schemas/base_response.py."
        )
        await self.orchestrator.handle(
            InboundMessage(69, 100, 200, "revisa el servicio de tareas")
        )
        await self.orchestrator.scheduler.wait_idle(timeout=2)
        await self.flush_all()

        publication = await self.orchestrator.handle(
            InboundMessage(70, 100, 200, "arma el PR")
        )

        self.assertIsNone(publication.job_id)
        self.assertEqual(self.worker.create_calls, [])
        self.assertEqual(await self.flush_all(), [JOB_NOT_FOUND_MESSAGE])

    async def test_research_does_not_guess_between_multiple_service_repositories(self) -> None:
        self.opencode.response = (
            "Ver brain-capnet/Inicio.md, customer-service/app.py y "
            "capnet-next-lambda-tasks/handler.py."
        )
        await self.orchestrator.handle(
            InboundMessage(33, 100, 200, "investiga la relación")
        )
        await self.orchestrator.scheduler.wait_idle(timeout=2)

        self.assertIsNone(self.storage.get_conversation(KEY).active_repository)

    async def test_codex_change_runs_preflight_polls_worker_and_prepares_branch(self) -> None:
        submission = await self.orchestrator.handle(
            InboundMessage(
                4,
                100,
                200,
                "agrega task_available en el repo capnet-next-lambda-tasks",
            )
        )
        await self.orchestrator.scheduler.wait_idle(timeout=2)
        sent = await self.flush_all()

        self.assertEqual(submission.intent, Intent.CODE_CHANGE)
        self.assertEqual(len(self.worker.create_calls), 1)
        call = self.worker.create_calls[0]
        self.assertEqual(call["repository"], "capnet-next-lambda-tasks")
        self.assertIn("preflight", call)
        self.assertEqual(call["policy"], self.orchestrator.instructions)
        self.assertIn("agrega task_available", self.opencode.calls[0][0])
        job = self.storage.get_job(submission.job_id)
        self.assertEqual(job.state, JobStatus.PREPARED)
        self.assertEqual(job.validation_status.value, "passed")
        self.assertTrue(job.branch.startswith("poo-ia/"))
        self.assertTrue(any("campo agregado" in text for text in sent))

    async def test_recovered_codex_gets_existing_worker_job_without_repeating_preflight(self) -> None:
        registration = self.storage.register_inbound(
            InboundMessage(40, 100, 200, "agrega el campo"),
            job_kind=JobKind.CODEX,
            repository="capnet-next-lambda-tasks",
            payload={
                "prompt": "agrega el campo",
                "conversation_context": "contexto anterior",
                "repository": "capnet-next-lambda-tasks",
                "publish": False,
                "skip_preflight": False,
            },
        )
        job = registration.job
        assert job is not None
        self.storage.transition_job(job.job_id, JobStatus.RUNNING)
        self.worker.polls[job.job_id] = [
            {
                "job_id": job.job_id,
                "state": "prepared",
                "repository": "capnet-next-lambda-tasks",
                "branch": f"poo-ia/{job.job_id[:8]}-change",
                "summary": "resultado ya existente",
                "validation": {"status": "passed"},
            }
        ]

        await self.orchestrator.start()
        await self.orchestrator.scheduler.wait_idle(timeout=2)

        self.assertEqual(self.worker.get_calls, [job.job_id])
        self.assertEqual(self.worker.create_calls, [])
        self.assertEqual(self.opencode.calls, [])
        self.assertEqual(self.storage.get_job(job.job_id).state, JobStatus.PREPARED)

    async def test_recovered_codex_reuses_its_snapshotted_trusted_policy(self) -> None:
        registration = self.storage.register_inbound(
            InboundMessage(65, 100, 200, "agrega el campo"),
            job_kind=JobKind.CODEX,
            repository="capnet-next-lambda-tasks",
            payload={
                "prompt": "agrega el campo",
                "conversation_context": "",
                "repository": "capnet-next-lambda-tasks",
                "publish": False,
                "skip_preflight": True,
                "policy": "politica confiable original",
            },
        )
        job = registration.job
        assert job is not None
        self.storage.transition_job(job.job_id, JobStatus.RUNNING)
        self.orchestrator.instructions = "politica cambiada tras reiniciar"

        await self.orchestrator.start()
        await self.orchestrator.scheduler.wait_idle(timeout=2)

        self.assertEqual(
            self.worker.create_calls[-1]["policy"], "politica confiable original"
        )

    async def test_auto_publication_rollback_stays_prepared_with_remote_error(self) -> None:
        registration = self.storage.register_inbound(
            InboundMessage(41, 100, 200, "agrega el campo y arma el PR"),
            job_kind=JobKind.CODEX,
            repository="capnet-next-lambda-tasks",
            payload={
                "prompt": "agrega el campo y arma el PR",
                "conversation_context": "",
                "repository": "capnet-next-lambda-tasks",
                "publish": True,
                "skip_preflight": False,
            },
        )
        job = registration.job
        assert job is not None
        self.storage.transition_job(job.job_id, JobStatus.RUNNING)
        self.worker.polls[job.job_id] = [
            {"job_id": job.job_id, "state": "publishing"},
            {
                "job_id": job.job_id,
                "state": "prepared",
                "repository": "capnet-next-lambda-tasks",
                "branch": f"poo-ia/{job.job_id[:8]}-change",
                "summary": "cambio listo",
                "error": "GitHub CLI is not authenticated",
                "validation": {"status": "passed"},
            },
        ]

        await self.orchestrator.start()
        await self.orchestrator.scheduler.wait_idle(timeout=2)

        recovered = self.storage.get_job(job.job_id)
        self.assertEqual(recovered.state, JobStatus.PREPARED)
        self.assertEqual(recovered.safe_error, "GitHub CLI is not authenticated")

    async def test_repository_inventory_can_refresh_after_worker_late_start(self) -> None:
        self.orchestrator.set_repositories(())
        before = await self.orchestrator.handle(
            InboundMessage(
                40,
                100,
                200,
                "agrega task_available en capnet-next-lambda-tasks",
            )
        )
        self.assertEqual(before.intent, Intent.CLARIFY)

        self.orchestrator.set_repositories(("capnet-next-lambda-tasks",))
        after = await self.orchestrator.handle(
            InboundMessage(
                41,
                100,
                200,
                "agrega task_available en capnet-next-lambda-tasks",
            )
        )
        await self.orchestrator.scheduler.wait_idle(timeout=2)

        self.assertEqual(after.intent, Intent.CODE_CHANGE)
        self.assertEqual(
            self.worker.create_calls[-1]["repository"],
            "capnet-next-lambda-tasks",
        )

    async def test_explicit_follow_up_pr_publishes_existing_job_and_returns_url(self) -> None:
        change = await self.orchestrator.handle(
            InboundMessage(
                5,
                100,
                200,
                "agrega task_available en el repo capnet-next-lambda-tasks",
            )
        )
        await self.orchestrator.scheduler.wait_idle(timeout=2)
        await self.flush_all()

        publication = await self.orchestrator.handle(
            InboundMessage(6, 100, 200, "arma el PR de ese trabajo")
        )
        await self.orchestrator.scheduler.wait_idle(timeout=2)
        sent = await self.flush_all()

        self.assertEqual(publication.intent, Intent.PULL_REQUEST)
        self.assertEqual(self.worker.publish_calls, [(change.job_id, False)])
        self.assertEqual(self.storage.get_job(change.job_id).state, JobStatus.SUCCEEDED)
        self.assertEqual(
            self.storage.get_job(publication.job_id).external_reference,
            "https://github.com/example/repo/pull/7",
        )
        self.assertTrue(any("https://github.com/example/repo/pull/7" in text for text in sent))

        repeated = await self.orchestrator.handle(
            InboundMessage(60, 100, 200, "arma el PR de ese trabajo")
        )
        await self.orchestrator.scheduler.wait_idle(timeout=2)

        self.assertEqual(self.storage.get_job(repeated.job_id).kind, JobKind.PUBLISH)
        self.assertEqual(
            self.worker.publish_calls,
            [(change.job_id, False), (change.job_id, False)],
        )
        self.assertEqual(len(self.worker.create_calls), 1)

    async def test_manual_publication_preserves_target_notification_ack_across_restart(self) -> None:
        change = await self.orchestrator.handle(
            InboundMessage(
                61,
                100,
                200,
                "agrega task_available en repo capnet-next-lambda-tasks",
            )
        )
        await self.orchestrator.scheduler.wait_idle(timeout=2)
        await self.flush_all()
        prepared = self.storage.get_job(change.job_id)
        assert prepared is not None
        self.assertIsNotNone(prepared.notification_completed_at)

        publication = await self.orchestrator.handle(
            InboundMessage(62, 100, 200, "arma el PR de ese trabajo")
        )
        await self.orchestrator.scheduler.wait_idle(timeout=2)
        await self.flush_all()

        target = self.storage.get_job(change.job_id)
        wrapper = self.storage.get_job(publication.job_id)
        assert target is not None and wrapper is not None
        self.assertEqual(target.state, JobStatus.SUCCEEDED)
        self.assertIsNotNone(target.notification_completed_at)
        self.assertIsNotNone(wrapper.notification_completed_at)
        self.assertEqual(self.orchestrator.pending_outputs(KEY), [])

        await self.orchestrator.close()
        restarted = PooIAOrchestrator(
            storage=self.storage,
            memory=self.memory,
            outbox=self.outbox,
            ollama=self.ollama,
            content_root=Path(self.temporary.name),
            opencode=self.opencode,
            worker=self.worker,
            repositories=("capnet-next-lambda-tasks",),
            worker_poll_seconds=0.001,
            clock=lambda: 1_000,
        )
        try:
            await restarted.start()
            await restarted.scheduler.wait_idle(timeout=2)
            self.assertEqual(restarted.pending_outputs(KEY), [])
        finally:
            await restarted.close()

    async def test_worker_sync_preserves_ack_without_post_transition_restore_window(self) -> None:
        registration = self.storage.register_inbound(
            InboundMessage(73, 100, 200, "agrega campo"),
            job_kind=JobKind.CODEX,
            repository="capnet-next-lambda-tasks",
        )
        target = registration.job
        assert target is not None
        self.storage.transition_job(target.job_id, JobStatus.RUNNING)
        self.storage.transition_job(target.job_id, JobStatus.PREPARED)
        acknowledged = self.storage.mark_job_notification_completed(target.job_id)

        synchronized = self.orchestrator._synchronize_worker_manifest(
            target.job_id,
            {
                "state": "succeeded",
                "summary": "publicado",
                "pr_url": "https://github.com/example/repo/pull/9",
            },
            preserve_notification_ack=True,
        )

        assert synchronized is not None
        self.assertEqual(synchronized.state, JobStatus.SUCCEEDED)
        self.assertEqual(
            synchronized.notification_completed_at,
            acknowledged.notification_completed_at,
        )
        self.assertEqual(self.storage.list_jobs_pending_notification(), [])

    async def test_cancelling_publish_accepts_remote_prepared_preservation(self) -> None:
        change = await self.orchestrator.handle(
            InboundMessage(
                63,
                100,
                200,
                "agrega task_available en repo capnet-next-lambda-tasks",
            )
        )
        await self.orchestrator.scheduler.wait_idle(timeout=2)
        target = self.storage.get_job(change.job_id)
        assert target is not None
        self.worker.cancel_result = {
            "job_id": target.job_id,
            "state": "prepared",
            "repository": "capnet-next-lambda-tasks",
            "branch": target.branch,
            "error": "Publication was cancelled; the prepared change was preserved.",
        }
        registration = self.storage.register_inbound(
            InboundMessage(64, 100, 200, "publica el cambio"),
            job_kind=JobKind.PUBLISH,
            repository="capnet-next-lambda-tasks",
            payload={"target_job_id": target.job_id, "override": False},
        )
        wrapper = registration.job
        assert wrapper is not None
        wrapper = self.storage.transition_job(wrapper.job_id, JobStatus.RUNNING)

        await self.orchestrator._cancel_external_job(wrapper)

        self.assertEqual(self.worker.cancel_calls[-1], target.job_id)
        self.assertEqual(self.storage.get_job(target.job_id).state, JobStatus.PREPARED)

    async def test_combined_change_and_pr_waits_through_transient_prepared_state(self) -> None:
        publication = await self.orchestrator.handle(
            InboundMessage(
                36,
                100,
                200,
                "agrega task_available y arma el PR en repo capnet-next-lambda-tasks",
            )
        )
        await self.orchestrator.scheduler.wait_idle(timeout=2)
        sent = await self.flush_all()

        job = self.storage.get_job(publication.job_id)
        self.assertEqual(job.state, JobStatus.SUCCEEDED)
        self.assertEqual(job.external_reference, "https://github.com/example/repo/pull/8")
        self.assertTrue(self.worker.create_calls[0]["publish"])
        self.assertTrue(any("pull/8" in text for text in sent))

    async def test_duplicate_transport_event_does_not_schedule_twice(self) -> None:
        message = InboundMessage(7, 100, 200, "analiza el servicio")
        first = await self.orchestrator.handle(message)
        duplicate = await self.orchestrator.handle(message)
        await self.orchestrator.scheduler.wait_idle(timeout=2)

        self.assertTrue(first.created)
        self.assertFalse(duplicate.created)
        self.assertEqual(first.job_id, duplicate.job_id)
        self.assertEqual(len(self.opencode.calls), 1)

    async def test_status_is_direct_and_refreshes_remote_worker_state(self) -> None:
        change = await self.orchestrator.handle(
            InboundMessage(
                8,
                100,
                200,
                "agrega task_available en repo capnet-next-lambda-tasks",
            )
        )
        await self.orchestrator.scheduler.wait_idle(timeout=2)
        await self.flush_all()

        status = await self.orchestrator.handle(
            InboundMessage(9, 100, 200, "cómo va")
        )
        sent = await self.flush_all()

        self.assertEqual(status.intent, Intent.JOB_STATUS)
        self.assertFalse(status.queued)
        self.assertTrue(any(change.job_id[:8] in text and "prepared" in text for text in sent))

    async def test_cancel_control_interrupts_active_research_without_waiting_in_queue(self) -> None:
        self.opencode.block = True
        research = await self.orchestrator.handle(
            InboundMessage(10, 100, 200, "investiga el servicio")
        )
        await asyncio.wait_for(self.opencode.started.wait(), timeout=1)

        cancellation = await self.orchestrator.handle(
            InboundMessage(11, 100, 200, "cancela ese trabajo")
        )
        await self.orchestrator.scheduler.wait_idle(timeout=2)
        sent = await self.flush_all()

        self.assertEqual(cancellation.intent, Intent.CANCEL)
        self.assertEqual(self.storage.get_job(research.job_id).state, JobStatus.CANCELLED)
        self.assertEqual(self.opencode.abort_calls, 1)
        self.assertTrue(any("cancelado" in text for text in sent))

    async def test_cancel_reports_remote_success_and_pr_instead_of_claiming_cancelled(self) -> None:
        registration = self.storage.register_inbound(
            InboundMessage(42, 100, 200, "agrega el campo"),
            job_kind=JobKind.CODEX,
            repository="capnet-next-lambda-tasks",
            payload={"repository": "capnet-next-lambda-tasks"},
        )
        job = registration.job
        assert job is not None
        self.storage.transition_job(job.job_id, JobStatus.RUNNING)
        self.worker.cancel_result = {
            "job_id": job.job_id,
            "state": "succeeded",
            "repository": "capnet-next-lambda-tasks",
            "branch": "poo-ia/done",
            "pr_url": "https://github.com/example/repo/pull/99",
        }

        text = await self.orchestrator._cancel_text(job)

        self.assertIn("succeeded", text)
        self.assertIn("https://github.com/example/repo/pull/99", text)
        self.assertNotIn("cancelado", text.casefold())

    async def test_forget_clears_acked_memory_and_active_references(self) -> None:
        await self.orchestrator.handle(InboundMessage(12, 100, 200, "hola"))
        await self.flush_all()
        self.storage.set_conversation_context(
            KEY, active_repository="customer-service", last_job_id="old-job"
        )

        forgotten = await self.orchestrator.handle(
            InboundMessage(13, 100, 200, "olvida la conversación")
        )
        await self.flush_all()
        snapshot = self.memory.snapshot(KEY)

        self.assertEqual(forgotten.intent, Intent.FORGET)
        self.assertEqual(snapshot.exchanges, ())
        self.assertIsNone(snapshot.conversation.active_repository)
        self.assertIsNone(snapshot.conversation.last_job_id)

    async def test_forget_during_research_prevents_late_result_from_restoring_context(self) -> None:
        self.opencode.response = "Ver capnet-next-lambda-tasks/schema.py"
        self.opencode.block = True
        await self.orchestrator.handle(
            InboundMessage(34, 100, 200, "investiga task_available")
        )
        await asyncio.wait_for(self.opencode.started.wait(), timeout=1)

        await self.orchestrator.handle(
            InboundMessage(35, 100, 200, "olvida la conversación")
        )
        self.opencode.release.set()
        await self.orchestrator.scheduler.wait_idle(timeout=2)
        await self.flush_all()

        snapshot = self.memory.snapshot(KEY)
        self.assertEqual(snapshot.exchanges, ())
        self.assertIsNone(snapshot.conversation.active_repository)
        self.assertIsNone(snapshot.conversation.last_job_id)

    async def test_aws_runs_only_on_host_and_never_enters_model_context(self) -> None:
        self.orchestrator.aws_enabled = True
        submission = await self.orchestrator.handle(InboundMessage(
            930, 100, 200, "describe la tabla Tasks en DynamoDB"
        ))
        self.assertEqual(submission.intent, Intent.AWS_REPORT)
        self.assertIsNone(submission.job_id)
        self.assertEqual(self.worker.aws_calls, [("describe-dynamodb", "Tasks", None)])
        self.assertEqual(self.opencode.calls, [])
        self.assertEqual(self.ollama.prompts, [])
        self.assertEqual(await self.flush_all(), ["Tabla Tasks: ACTIVE (metadatos)."])
        self.assertEqual(self.memory.snapshot(KEY).exchanges, ())
        self.assertIsNone(self.storage.get_conversation(KEY).active_repository)
        self.assertEqual(self.storage.list_jobs(), [])

    async def test_duplicate_aws_request_is_idempotent_and_preserves_execution_context(self) -> None:
        self.orchestrator.aws_enabled = True
        self.storage.set_conversation_context(KEY, active_repository="capnet-next-lambda-tasks")
        message = InboundMessage(933, 100, 200, "lista tablas de DynamoDB")
        first = await self.orchestrator.handle(message)
        await self.flush_all()
        duplicate = await self.orchestrator.handle(message)
        self.assertTrue(first.created)
        self.assertFalse(duplicate.created)
        self.assertEqual(self.worker.aws_calls, [("list-dynamodb", None, None)])
        self.assertEqual(await self.flush_all(), [])
        self.assertEqual(self.memory.snapshot(KEY).exchanges, ())
        self.assertEqual(self.storage.get_conversation(KEY).active_repository, "capnet-next-lambda-tasks")

    async def test_record_and_log_reads_stay_out_of_models_memory_and_brain(self) -> None:
        self.orchestrator.aws_enabled = True
        for message_id, text in ((936, "consulta registros de la tabla Tasks en DynamoDB"), (937, "ver logs del grupo /aws/lambda/tasks en CloudWatch")):
            await self.orchestrator.handle(InboundMessage(message_id, 100, 200, text))
            await self.flush_all()
        self.assertEqual(self.worker.aws_calls, [("scan-dynamodb", "Tasks", None), ("read-logs", None, "/aws/lambda/tasks")])
        self.assertEqual(self.opencode.calls, [])
        self.assertEqual(self.ollama.prompts, [])
        self.assertEqual(self.memory.snapshot(KEY).exchanges, ())
        self.assertEqual(self.storage.list_jobs(), [])

    async def test_identity_and_lambda_requests_do_not_reach_worker(self) -> None:
        self.orchestrator.aws_enabled = True
        for message_id, text in ((938, "consulta mi identidad AWS"), (939, "lista las lambdas")):
            await self.orchestrator.handle(InboundMessage(message_id, 100, 200, text))
            self.assertIn("DynamoDB y CloudWatch", " ".join(await self.flush_all()))
        self.assertEqual(self.worker.aws_calls, [])

    async def test_capabilities_announce_aws_only_when_enabled(self) -> None:
        await self.orchestrator.handle(InboundMessage(934, 100, 200, "¿Qué puedes hacer?"))
        self.assertNotIn("consultar DynamoDB", " ".join(await self.flush_all()))
        self.orchestrator.aws_enabled = True
        await self.orchestrator.handle(InboundMessage(935, 100, 200, "¿Qué puedes hacer?"))
        self.assertIn("consultar DynamoDB", " ".join(await self.flush_all()))
        self.assertEqual(self.memory.snapshot(KEY).exchanges, ())

    async def test_aws_unsupported_requests_explain_allowlist_without_a_model(self) -> None:
        self.orchestrator.aws_enabled = True
        await self.orchestrator.handle(InboundMessage(931, 100, 200, "elimina todas las tablas de DynamoDB"))
        self.assertEqual(self.worker.aws_calls, [])
        self.assertEqual(self.opencode.calls, [])
        self.assertIn("Operaciones disponibles", " ".join(await self.flush_all()))
        self.assertEqual(self.memory.snapshot(KEY).exchanges, ())

    async def test_aws_transport_failure_never_echoes_raw_error(self) -> None:
        self.orchestrator.aws_enabled = True
        async def fail(*args, **kwargs):
            raise RuntimeError("raw-credential-value")
        self.worker.query_aws = fail
        await self.orchestrator.handle(InboundMessage(932, 100, 200, "lista tablas de DynamoDB"))
        text = " ".join(await self.flush_all())
        self.assertNotIn("raw-credential-value", text)
        self.assertIn("No pude", text)
        self.assertEqual(self.memory.snapshot(KEY).exchanges, ())

    async def test_aws_is_explicitly_disabled_without_touching_worker(self) -> None:
        result = await self.orchestrator.handle(
            InboundMessage(14, 100, 200, "haz un reporte de DynamoDB")
        )
        sent = await self.flush_all()

        self.assertEqual(result.intent, Intent.AWS_REPORT)
        self.assertTrue(any("desactivadas" in text for text in sent))
        self.assertEqual(self.worker.create_calls, [])

    async def test_wait_for_output_observes_background_completion(self) -> None:
        await self.orchestrator.handle(
            InboundMessage(15, 100, 200, "investiga el endpoint")
        )
        await self.flush_all()  # queue acknowledgement
        self.assertTrue(await self.orchestrator.wait_for_output(timeout=2))
        self.assertTrue(self.orchestrator.pending_outputs(KEY))
