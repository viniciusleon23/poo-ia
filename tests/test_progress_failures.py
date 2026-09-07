"""Feedback failures must not fail or abandon the operation being observed."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.memory import MemoryStore
from app.models import InboundMessage, Intent, JobKind, JobStatus
from app.orchestrator import PooIAOrchestrator
from app.outbox import DurableOutbox
from app.storage import SQLiteStorage, StorageError
from app.worker_client import WorkerNotFoundError
from tests.test_orchestrator import FakeOllama, FakeOpenCode, FakeWorker


class SlowPhaseWorker(FakeWorker):
    """Expose editing either at job creation or on the first status poll."""

    def __init__(self, phase_source: str):
        super().__init__()
        self.phase_source = phase_source
        self.created = False
        self.reported_phase = False
        self.waiting = asyncio.Event()
        self.release = asyncio.Event()

    async def create_codex_job(self, **kwargs):
        self.create_calls.append(dict(kwargs))
        self.created = True
        state = "running" if self.phase_source == "create" else "queued"
        return {"job_id": kwargs["job_id"], "state": state, "phase": "edit" if state == "running" else None}

    async def get_job(self, job_id):
        self.get_calls.append(job_id)
        if not self.created:
            raise WorkerNotFoundError("Job has not been submitted yet")
        if self.phase_source == "get" and not self.reported_phase:
            self.reported_phase = True
            return {"job_id": job_id, "state": "running", "phase": "edit"}
        self.waiting.set()
        await self.release.wait()
        return {
            "job_id": job_id, "state": "prepared", "summary": "Cambio preparado.",
            "repository": "capnet-next-lambda-tasks", "validation": {"status": "passed"},
        }


class ProgressFailureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.now = 100.0
        self.storage = SQLiteStorage(Path(self.directory.name) / "state.sqlite3", clock=lambda: self.now)
        self.outbox = DurableOutbox(self.storage, clock=lambda: self.now)
        self.research = FakeOpenCode()
        self.core = PooIAOrchestrator(
            storage=self.storage, memory=MemoryStore(self.storage), outbox=self.outbox,
            ollama=FakeOllama(), opencode=self.research, worker=FakeWorker(),
            content_root=Path(self.directory.name), repositories=("capnet-next-lambda-tasks",),
            clock=lambda: self.now, progress_interval_seconds=0.01, worker_poll_seconds=0.005,
        )

    async def asyncTearDown(self) -> None:
        await self.core.close()
        self.storage.close()
        self.directory.cleanup()

    async def test_research_continues_and_finishes_when_stage_output_fails_once(self) -> None:
        self.research.block = True
        original = self.outbox.enqueue_progress
        attempts = []
        retried = asyncio.Event()

        def transient_failure(*args, **kwargs):
            attempts.append(kwargs["dedupe_key"])
            if len(attempts) == 1:
                raise StorageError("Transient stage output failure")
            result = original(*args, **kwargs)
            retried.set()
            return result

        with patch.object(self.outbox, "enqueue_progress", side_effect=transient_failure):
            submission = await self.core.handle(InboundMessage(901, 100, 200, "investiga las tareas"))
            await asyncio.wait_for(self.research.started.wait(), 1)
            self.assertEqual(self.storage.get_job(submission.job_id).state, JobStatus.RUNNING)
            self.assertEqual(len(self.research.calls), 1)
            await asyncio.wait_for(retried.wait(), 2)
            self.assertEqual(attempts[0], attempts[1])
            self.research.release.set()
            await self.core.scheduler.wait_idle(timeout=2)

        job = self.storage.get_job(submission.job_id)
        self.assertEqual(job.state, JobStatus.SUCCEEDED)
        self.assertIsNone(job.safe_error)
        self.assertEqual(self.research.abort_calls, 0)
        self.assertEqual(len(self.research.calls), 1)
        self.assertTrue(self.storage.request_has_final_outbox(job.request_id))
        self.assertFalse(any("Transient" in part.content for part in self.outbox.pending()))

    async def test_worker_created_remotely_is_not_abandoned_after_stage_output_failure(self) -> None:
        await self._worker_survives_stage_failure("create")

    async def test_worker_polled_remotely_is_not_abandoned_after_stage_output_failure(self) -> None:
        await self._worker_survives_stage_failure("get")

    async def _worker_survives_stage_failure(self, phase_source: str) -> None:
        worker = SlowPhaseWorker(phase_source)
        self.core.worker = worker
        original = self.outbox.enqueue_progress
        failures = []

        def transient_failure(*args, **kwargs):
            if kwargs["dedupe_key"].endswith("-edit") and not failures:
                failures.append(kwargs["dedupe_key"])
                raise StorageError("Transient editing feedback failure")
            return original(*args, **kwargs)

        with patch.object(self.outbox, "enqueue_progress", side_effect=transient_failure):
            submission = await self.core.handle(InboundMessage(
                902, 100, 200, "agrega task_available en capnet-next-lambda-tasks sin investigar",
            ))
            await asyncio.wait_for(worker.waiting.wait(), 1)
            self.assertEqual(len(failures), 1)
            self.assertEqual(len(worker.create_calls), 1)
            self.assertEqual(self.storage.get_job(submission.job_id).state, JobStatus.RUNNING)
            worker.release.set()
            await self.core.scheduler.wait_idle(timeout=2)

        job = self.storage.get_job(submission.job_id)
        self.assertEqual(job.state, JobStatus.PREPARED)
        self.assertIsNone(job.safe_error)
        self.assertEqual(worker.cancel_calls, [])
        self.assertEqual(len(worker.create_calls), 1)
        self.assertGreaterEqual(len(worker.get_calls), 2)
        self.assertEqual(self.research.calls, [])
        self.assertTrue(self.storage.request_has_final_outbox(job.request_id))
        pending_before_retry = self.outbox.pending()
        self.core.progress.emit_heartbeats()
        self.assertEqual(self.outbox.pending(), pending_before_retry)

    def _active_job(self):
        job = self.storage.register_inbound(
            InboundMessage(903, 100, 200, "investiga las tareas"),
            intent=Intent.RESEARCH, job_kind=JobKind.RESEARCH,
        ).job
        return self.storage.transition_job(job.job_id, JobStatus.RUNNING)

    async def test_monitor_retries_missing_stage_without_changing_persisted_checkpoint(self) -> None:
        job = self._active_job()
        self.storage.update_running_job_checkpoint(job.job_id, {"opencode_session_id": "owned-session"})
        original = self.outbox.enqueue_progress
        attempts = []
        retried = asyncio.Event()

        def transient_failure(*args, **kwargs):
            attempts.append((args, dict(kwargs)))
            if len(attempts) == 1:
                raise StorageError("Transient stage output failure")
            result = original(*args, **kwargs)
            retried.set()
            return result

        with patch.object(self.outbox, "enqueue_progress", side_effect=transient_failure):
            self.core.progress.phase(job.job_id, "research")
            checkpoint = dict(self.storage.get_job(job.job_id).checkpoint)
            self.assertEqual(checkpoint["progress_revision"], 1)
            self.assertEqual(checkpoint["progress_at"], 100)
            self.assertEqual(self.outbox.pending(), [])
            self.core.progress.start()
            await asyncio.wait_for(retried.wait(), 2)
            self.assertEqual(self.storage.get_job(job.job_id).checkpoint, checkpoint)
            self.assertEqual(attempts[0], attempts[1])
            part = self.outbox.pending()[0]
            self.outbox.acknowledge(part, "stage-delivered")
            self.core.progress.phase(job.job_id, "research")
            self.assertEqual(self.outbox.pending(), [])
        rows = self.storage._connection.execute(
            "SELECT COUNT(*) FROM outbox WHERE request_id = ? AND kind = 'progress'", (job.request_id,),
        ).fetchone()[0]
        self.assertEqual(rows, 1)

    async def test_monitor_retries_transient_checkpoint_failure_and_preserves_ownership(self) -> None:
        job = self._active_job()
        original = self.storage.update_running_job_checkpoint
        original(job.job_id, {"opencode_session_id": "owned-session"})
        attempts = []
        persisted = asyncio.Event()

        def transient_failure(*args, **kwargs):
            attempts.append((args, kwargs))
            if len(attempts) == 1:
                raise StorageError("Transient checkpoint failure")
            result = original(*args, **kwargs)
            persisted.set()
            return result

        with patch.object(self.storage, "update_running_job_checkpoint", side_effect=transient_failure):
            self.core.progress.phase(job.job_id, "research")
            self.assertEqual(self.storage.get_job(job.job_id).checkpoint, {"opencode_session_id": "owned-session"})
            self.assertEqual(self.outbox.pending(), [])
            self.core.progress.start()
            await asyncio.wait_for(persisted.wait(), 2)
        checkpoint = self.storage.get_job(job.job_id).checkpoint
        self.assertEqual(checkpoint["opencode_session_id"], "owned-session")
        self.assertEqual(checkpoint["progress_phase"], "research")
        self.assertEqual(checkpoint["progress_revision"], 1)
        self.assertEqual(self.storage.get_job(job.job_id).state, JobStatus.RUNNING)
        self.assertEqual(len(self.outbox.pending()), 1)

