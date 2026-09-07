"""Offline feedback delivery while real orchestration and recovery are in flight."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from app.memory import MemoryStore
from app.models import InboundMessage, Intent, JobStatus
from app.orchestrator import PooIAOrchestrator
from app.outbox import DurableOutbox
from app.storage import SQLiteStorage
from tests.test_discord_delivery import AdapterClient, FakeChannel
from tests.test_orchestrator import FakeOllama, FakeOpenCode, FakeWorker


class SlowAwsWorker(FakeWorker):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def query_aws(self, action, **kwargs):
        report = await super().query_aws(action, **kwargs)
        self.started.set()
        await self.release.wait()
        return report


class ProgressFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.worker = SlowAwsWorker()
        self.ollama = FakeOllama()
        self.opencode = FakeOpenCode()
        self.channel = FakeChannel()
        self.adapter = AdapterClient(self.channel)
        self.storage = None
        self.orchestrator = None
        self.open()

    def open(self):
        self.storage = SQLiteStorage(self.root / "state.sqlite3")
        self.orchestrator = PooIAOrchestrator(
            storage=self.storage, memory=MemoryStore(self.storage),
            outbox=DurableOutbox(self.storage), ollama=self.ollama,
            opencode=self.opencode, worker=self.worker, content_root=self.root,
            aws_enabled=True, receipt_delay_seconds=0.005,
            progress_interval_seconds=0.03,
        )

    async def shutdown(self):
        if self.orchestrator is not None:
            await self.orchestrator.close()
            self.orchestrator = None
        if self.storage is not None:
            self.storage.close()
            self.storage = None

    async def asyncTearDown(self) -> None:
        await self.shutdown()
        await self.adapter.close()
        self.temporary.cleanup()

    async def deliver_until(self, predicate):
        async def wait_for_delivery():
            while not predicate():
                await self.orchestrator.wait_for_output(timeout=0.1)
                await self.orchestrator.flush_outputs(self.adapter._send_outbox_part)
        await asyncio.wait_for(wait_for_delivery(), timeout=2)

    async def test_slow_aws_receipt_delivers_during_handler_and_restart_recovers_only_missing_result(self) -> None:
        message = InboundMessage(801, 100, 200, "lista las tablas DynamoDB")
        operation = asyncio.create_task(self.orchestrator.handle(message))
        try:
            await asyncio.wait_for(self.worker.started.wait(), 1)
            await self.deliver_until(lambda: bool(self.channel.sent))
            self.assertFalse(operation.done())
            self.assertIn("Recibí tu consulta", self.channel.sent[0])
            self.assertEqual(self.storage.list_exchanges(message.conversation_key), [])
            registration = self.storage.register_inbound(message)
            self.assertTrue(self.storage.request_has_outbox(registration.request.request_id))
            self.assertFalse(self.storage.request_has_final_outbox(registration.request.request_id))
        finally:
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
        await self.shutdown()

        # The receipt was ACKed, but there is no AWS result to recover yet.
        self.worker.release.set()
        self.open()
        await self.orchestrator.recover()
        self.assertEqual(len(self.worker.aws_calls), 2)
        pending = self.orchestrator.pending_outputs(message.conversation_key)
        self.assertEqual(len(pending), 1)
        final_id = pending[0].outbox_id
        self.assertEqual(self.storage.get_outbox(final_id).kind, "response")
        await self.shutdown()

        # Once the result is durable, another restart reuses it without AWS.
        self.open()
        await self.orchestrator.recover()
        duplicate = await self.orchestrator.handle(message)
        self.assertFalse(duplicate.created)
        self.assertEqual(len(self.worker.aws_calls), 2)
        self.assertEqual(self.orchestrator.pending_outputs()[0].outbox_id, final_id)
        await self.orchestrator.flush_outputs(self.adapter._send_outbox_part)
        self.assertEqual(len(self.channel.sent), 2)
        self.assertIn("Tabla Tasks", self.channel.sent[-1])
        self.assertEqual(self.storage.list_exchanges(message.conversation_key), [])
        self.assertEqual(self.ollama.prompts, [])
        self.assertEqual(self.opencode.calls, [])

    async def test_slow_research_heartbeats_and_queued_second_job_keep_one_execution_slot(self) -> None:
        self.opencode.block = True
        first = await self.orchestrator.handle(InboundMessage(802, 100, 200, "investiga cómo funcionan las tareas"))
        self.assertEqual(first.intent, Intent.RESEARCH)
        await asyncio.wait_for(self.opencode.started.wait(), 1)
        second = await self.orchestrator.handle(InboundMessage(803, 100, 200, "investiga cómo se consultan los clientes"))

        def received_both_heartbeats():
            return all(any(
                job_id[:8] in text and "Última etapa confirmada" in text
                for text in self.channel.sent
            ) for job_id in (first.job_id, second.job_id))

        await self.deliver_until(received_both_heartbeats)
        self.assertEqual(len(self.opencode.calls), 1)
        self.assertEqual(self.storage.get_job(first.job_id).state, JobStatus.RUNNING)
        self.assertEqual(self.storage.get_job(second.job_id).state, JobStatus.QUEUED)
        self.assertEqual(self.storage.get_job(second.job_id).attempts, 0)
        self.assertTrue(any(first.job_id[:8] in text and "documentación del brain" in text for text in self.channel.sent))
        self.assertTrue(any(second.job_id[:8] in text and "esperando turno" in text for text in self.channel.sent))
        self.assertEqual(self.storage.list_exchanges(InboundMessage(802, 100, 200, "unused").conversation_key), [])

        self.opencode.release.set()
        await self.orchestrator.scheduler.wait_idle(timeout=2)
        await self.orchestrator.flush_outputs(self.adapter._send_outbox_part)
        delivered = list(self.channel.sent)
        await asyncio.sleep(0.08)
        await self.orchestrator.flush_outputs(self.adapter._send_outbox_part)
        self.assertEqual(self.channel.sent, delivered)
        self.assertEqual(len(self.opencode.calls), 2)
        self.assertEqual(self.opencode.maximum_active, 1)
        self.assertTrue(all(job.state is JobStatus.SUCCEEDED for job in self.storage.list_jobs()))
        exchanges = self.storage.list_exchanges(InboundMessage(802, 100, 200, "unused").conversation_key)
        self.assertEqual(len(exchanges), 2)
        self.assertTrue(all("Última etapa" not in exchange.assistant_text for exchange in exchanges))

    async def test_cancellation_ends_research_feedback_and_final_suppresses_stale_progress(self) -> None:
        self.opencode.block = True
        started = await self.orchestrator.handle(InboundMessage(804, 100, 200, "investiga las tareas"))
        await asyncio.wait_for(self.opencode.started.wait(), 1)
        await self.orchestrator.handle(InboundMessage(805, 100, 200, f"cancela el trabajo {started.job_id}"))
        await self.orchestrator.scheduler.wait_idle(timeout=2)
        await self.orchestrator.flush_outputs(self.adapter._send_outbox_part)
        self.assertEqual(self.storage.get_job(started.job_id).state, JobStatus.CANCELLED)
        self.assertEqual(self.opencode.abort_calls, 1)
        delivered = list(self.channel.sent)
        await asyncio.sleep(0.08)
        await self.orchestrator.flush_outputs(self.adapter._send_outbox_part)
        self.assertEqual(self.channel.sent, delivered)
        self.assertFalse(any("Última etapa confirmada" in text for text in self.channel.sent))
