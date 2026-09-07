from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from app.models import ConversationKey, InboundMessage, Intent, JobKind, JobStatus
from app.outbox import DurableOutbox
from app.progress import ProgressReporter
from app.storage import SQLiteStorage


KEY = ConversationKey("discord", 100, 200)


class ProgressReporterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.storage = SQLiteStorage(":memory:")
        self.now = 100.0
        self.outbox = DurableOutbox(self.storage, clock=lambda: self.now)
        self.wake = asyncio.Event()
        self.reporter = ProgressReporter(
            self.storage, self.outbox, self.wake.set, clock=lambda: self.now,
            interval_seconds=60, receipt_delay_seconds=0.005,
        )

    async def asyncTearDown(self) -> None:
        await self.reporter.close()
        self.storage.close()

    def job(self, message_id=1, state=JobStatus.RUNNING):
        job = self.storage.register_inbound(
            InboundMessage(message_id, 100, 200, "consulta documentación"),
            intent=Intent.RESEARCH, job_kind=JobKind.RESEARCH, now=self.now,
        ).job
        if state is JobStatus.QUEUED:
            return job
        self.storage.transition_job(job.job_id, JobStatus.RUNNING, now=self.now)
        if state is JobStatus.PUBLISHING:
            self.storage.transition_job(job.job_id, JobStatus.PREPARED, now=self.now)
        if state is not JobStatus.RUNNING:
            self.storage.transition_job(job.job_id, state, now=self.now)
        return self.storage.get_job(job.job_id)

    def request(self, message_id=101):
        return self.storage.register_inbound(
            InboundMessage(message_id, 100, 200, "lista tablas DynamoDB"),
            intent=Intent.AWS_REPORT, now=self.now,
        ).request.request_id

    def acknowledge_pending(self):
        for index, part in enumerate(self.outbox.pending()):
            self.outbox.acknowledge(part, f"sent-{index}", now=self.now)

    async def test_phase_checkpoints_before_delivery_and_preserves_existing_ownership(self) -> None:
        job = self.job()
        self.storage.update_running_job_checkpoint(job.job_id, {"opencode_session_id": "session-owned"})
        enqueue = self.outbox.enqueue_progress
        observed = []

        def check_checkpoint(*args, **kwargs):
            observed.append(dict(self.storage.get_job(job.job_id).checkpoint))
            return enqueue(*args, **kwargs)

        with patch.object(self.outbox, "enqueue_progress", side_effect=check_checkpoint):
            self.reporter.phase(job.job_id, "research")
            self.reporter.phase(job.job_id, "research")
            self.reporter.phase(job.job_id, "untrusted raw model status")
        self.assertEqual(observed, [{
            "opencode_session_id": "session-owned", "progress_phase": "research", "progress_at": 100.0, "progress_revision": 1,
        }])
        self.assertEqual(len(self.outbox.pending()), 1)
        self.assertIn("documentación del brain", self.outbox.pending()[0].content)
        self.assertTrue(self.wake.is_set())
        self.acknowledge_pending()
        self.assertEqual(self.storage.list_exchanges(KEY), [])

    async def test_heartbeat_waits_full_interval_and_deduplicates_after_reporter_restart(self) -> None:
        job = self.job()
        self.reporter.phase(job.job_id, "validate")
        self.acknowledge_pending()
        self.now = 159.999
        self.reporter.emit_heartbeats()
        self.assertEqual(self.outbox.pending(), [])
        self.now = 160
        self.reporter.emit_heartbeats()
        first = self.outbox.pending()[0]
        self.assertIn("Última etapa confirmada", first.content)
        self.assertIn("pruebas", first.content)
        self.acknowledge_pending()
        await self.reporter.close()
        self.reporter = ProgressReporter(
            self.storage, self.outbox, self.wake.set, clock=lambda: self.now,
        )
        self.reporter.emit_heartbeats()
        self.assertEqual(self.outbox.pending(), [])
        self.now = 220
        self.reporter.emit_heartbeats()
        self.assertEqual(len(self.outbox.pending()), 1)
        self.assertNotEqual(self.outbox.pending()[0].outbox_id, first.outbox_id)

    async def test_queued_heartbeat_reports_waiting_without_claiming_execution(self) -> None:
        job = self.job(state=JobStatus.QUEUED)
        self.now = 160
        self.reporter.emit_heartbeats()
        self.assertIn("esperando turno para comenzar", self.outbox.pending()[0].content)
        self.assertEqual(self.storage.get_job(job.job_id).state, JobStatus.QUEUED)
        self.assertEqual(self.storage.get_job(job.job_id).attempts, 0)

    async def test_publishing_phase_is_persisted_and_reported(self) -> None:
        job = self.job(state=JobStatus.PUBLISHING)
        self.reporter.phase(job.job_id, "publish")
        current = self.storage.get_job(job.job_id)
        self.assertEqual(current.state, JobStatus.PUBLISHING)
        self.assertEqual(current.checkpoint["progress_phase"], "publish")
        self.assertIn("publicando el PR", self.outbox.pending()[0].content)

    async def test_prepared_terminal_and_final_owned_jobs_never_emit_late_progress(self) -> None:
        for number, state in enumerate((JobStatus.PREPARED, JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED), 1):
            self.job(number, state=state)
        active = self.job(10)
        self.reporter.phase(active.job_id, "edit")
        final = self.outbox.enqueue(active.request_id, "Resultado confirmado", remember_exchange=False)
        self.now += 120
        for job in self.storage.list_jobs():
            self.reporter.phase(job.job_id, "validate")
        self.reporter.emit_heartbeats()
        self.assertEqual({part.outbox_id for part in self.outbox.pending()}, {final.outbox_id})

    async def test_fast_direct_operation_does_not_send_delayed_receipt(self) -> None:
        request = self.request()
        async with self.reporter.direct(request, Intent.AWS_REPORT):
            pass
        await asyncio.sleep(0.02)
        self.assertEqual(self.outbox.pending(), [])
        self.assertFalse(self.wake.is_set())

    async def test_direct_receipt_and_heartbeat_stop_on_cancellation_without_memory(self) -> None:
        request = self.request()
        self.reporter.interval_seconds = 0.01

        async def waiting_operation():
            async with self.reporter.direct(request, Intent.AWS_REPORT):
                await asyncio.Event().wait()

        operation = asyncio.create_task(waiting_operation())
        try:
            await asyncio.wait_for(self.wake.wait(), 1)
            receipt = self.outbox.pending()[0]
            self.assertEqual(self.storage.get_outbox(receipt.outbox_id).kind, "ack")
            self.assertFalse(self.storage.request_has_final_outbox(request))
            self.acknowledge_pending()
            self.wake.clear()
            await asyncio.wait_for(self.wake.wait(), 1)
            progress = self.outbox.pending()[0]
            self.assertEqual(self.storage.get_outbox(progress.outbox_id).kind, "progress")
            self.assertIn("Aún no tengo un resultado confirmado", progress.content)
        finally:
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
        self.acknowledge_pending()
        await asyncio.sleep(0.03)
        self.assertEqual(self.outbox.pending(), [])
        self.assertEqual(self.storage.list_exchanges(KEY), [])

    async def test_close_stops_active_direct_notifier_and_monitor(self) -> None:
        request = self.request()
        self.reporter.interval_seconds = 0.01
        release = asyncio.Event()

        async def waiting_operation():
            async with self.reporter.direct(request, Intent.AWS_REPORT):
                await release.wait()

        self.reporter.start()
        self.reporter.start()
        operation = asyncio.create_task(waiting_operation())
        try:
            await asyncio.wait_for(self.wake.wait(), 1)
            await self.reporter.close()
            self.acknowledge_pending()
            await asyncio.sleep(0.03)
            self.assertEqual(self.outbox.pending(), [])
        finally:
            release.set()
            await operation
