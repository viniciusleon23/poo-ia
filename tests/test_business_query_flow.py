from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.memory import MemoryStore
from app.models import Backend, ConversationKey, CsvAttachment, InboundMessage, Intent, RequestStatus
from app.orchestrator import PooIAOrchestrator
from app.outbox import DurableOutbox
from app.storage import SQLiteStorage
from tests.test_business_queries import REAL_REQUEST
from tests.test_orchestrator import FakeOllama, FakeOpenCode, FakeWorker


KEY = ConversationKey("discord", 100, 200)


class BusinessWorker(FakeWorker):
    def __init__(self):
        super().__init__()
        self.business_calls = []
        self.report = None

    async def query_aws(self, action, *, table=None, log_group=None, output_format="text", business_query=None):
        if action != "count-planned-tasks":
            return await super().query_aws(action, table=table, log_group=log_group, output_format=output_format)
        self.business_calls.append((action, dict(business_query), output_format, table, log_group))
        if self.report is not None:
            return self.report
        result = {"state": "succeeded", "message": "12 tareas planeadas. Conteo completo; todos los tipos, estados y usuarios del dealer."}
        if output_format == "csv":
            result["attachment"] = CsvAttachment("planned-tasks.csv", b"count,complete\r\n12,true\r\n")
        return result


class BusinessQueryFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name)
        self.now = 1_788_755_400.0
        self.worker, self.ollama, self.docs = BusinessWorker(), FakeOllama(), FakeOpenCode()
        self.open()

    def open(self):
        self.storage = SQLiteStorage(self.path / "state.sqlite3", clock=lambda: self.now)
        self.outbox = DurableOutbox(self.storage, clock=lambda: self.now)
        self.core = PooIAOrchestrator(
            storage=self.storage, memory=MemoryStore(self.storage), outbox=self.outbox,
            ollama=self.ollama, opencode=self.docs, worker=self.worker, content_root=self.path,
            repositories=("brain-capnet", "capnet-next-lambda-tasks"), aws_enabled=True,
            clock=lambda: self.now, worker_poll_seconds=0.001,
        )

    async def shutdown(self):
        await self.core.close()
        self.storage.close()

    async def asyncTearDown(self) -> None:
        await self.shutdown()
        self.directory.cleanup()

    def acknowledge_all(self):
        for index, part in enumerate(self.outbox.pending()):
            self.outbox.acknowledge(part, f"sent-{index}")

    async def test_real_request_calls_worker_once_and_never_calls_models_or_creates_jobs(self) -> None:
        message = InboundMessage(1, 100, 200, REAL_REQUEST, received_at=self.now - 10)
        submission = await self.core.handle(message)
        duplicate = await self.core.handle(message)
        self.assertEqual(submission.intent, Intent.AWS_REPORT)
        self.assertFalse(duplicate.created)
        self.assertIsNone(submission.job_id)
        self.assertEqual(self.worker.business_calls, [(
            "count-planned-tasks", {"dealer_id": "COMAZDCALC2", "day": "tomorrow", "requested_at": self.now - 10},
            "text", None, None,
        )])
        self.assertIn("12 tareas", self.outbox.pending()[0].content)
        self.acknowledge_all()
        self.assertEqual(self.storage.list_exchanges(KEY), [])
        self.assertEqual(self.storage.list_jobs(), [])
        self.assertEqual(self.ollama.prompts, [])
        self.assertEqual(self.docs.calls, [])

    async def test_recognized_unsupported_requests_produce_clarification_without_backend_calls(self) -> None:
        for index, text in enumerate((
            "Cuenta tareas pendientes mañana del dealer ABC123",
            "Cuenta mis tareas mañana del dealer ABC123",
            "Cuenta tareas de asesor mañana del dealer ABC123",
            "Cuenta tareas pasado mañana del dealer ABC123",
            "Cuenta tareas mañana y ayer del dealer ABC123",
            "Cuenta tareas mañana",
            "consulta el estado de las tareas del dealer ABC123 para mañana",
        ), start=10):
            submission = await self.core.handle(InboundMessage(index, 100, 200, text))
            self.assertEqual(submission.intent, Intent.AWS_REPORT)
            self.assertIsNone(submission.job_id)
        self.assertEqual(self.worker.business_calls, [])
        self.assertEqual(self.worker.aws_calls, [])
        self.assertEqual(self.docs.calls, [])
        self.assertEqual(self.ollama.prompts, [])
        self.assertEqual(self.storage.list_jobs(), [])
        self.assertTrue(all("tablas de DynamoDB" not in part.content for part in self.outbox.pending()))
        self.acknowledge_all()
        self.assertEqual(self.storage.list_exchanges(KEY), [])

    async def test_documentation_question_remains_research(self) -> None:
        submission = await self.core.handle(InboundMessage(20, 100, 200, "Cómo funciona el conteo de tareas por dealer en el código"))
        await self.core.scheduler.wait_idle()
        self.assertEqual(submission.intent, Intent.RESEARCH)
        self.assertEqual(len(self.docs.calls), 1)
        self.assertEqual(self.worker.business_calls, [])

    async def test_direct_csv_and_followups_keep_original_reference_after_midnight(self) -> None:
        original_time = self.now
        await self.core.handle(InboundMessage(30, 100, 200, REAL_REQUEST + " en csv"))
        csv = self.outbox.pending()[0].attachment
        self.assertIsNotNone(csv)
        self.acknowledge_all()
        self.now += 86_400
        await self.core.handle(InboundMessage(31, 100, 200, "dámelo en csv"))
        self.assertEqual(self.worker.business_calls[-1][1]["requested_at"], original_time)
        self.assertEqual(self.worker.business_calls[-1][1]["day"], "tomorrow")
        self.assertEqual(self.outbox.pending()[0].attachment, csv)
        self.assertIn("Volví a consultar", self.outbox.pending()[0].content)
        self.acknowledge_all()
        self.now += 86_400
        await self.core.handle(InboundMessage(32, 100, 200, "en csv"))
        self.assertEqual(self.worker.business_calls[-1][1]["requested_at"], original_time)
        self.assertEqual([call[2] for call in self.worker.business_calls], ["csv", "csv", "csv"])
        self.assertEqual(self.docs.calls, [])
        self.assertEqual(self.storage.list_exchanges(KEY), [])

    async def test_recovery_uses_persisted_reference_and_reuses_final_csv_without_requery(self) -> None:
        original_time = self.now
        message = InboundMessage(40, 100, 200, REAL_REQUEST + " en csv", received_at=original_time)
        registration = self.storage.register_inbound(message, intent=Intent.AWS_REPORT, backend=Backend.WORKER)
        self.outbox.enqueue(registration.request.request_id, "Recibido.", kind="ack", dedupe_key="received", remember_exchange=False)
        self.acknowledge_all()
        await self.shutdown()
        self.now += 86_400
        self.open()
        await self.core.recover()
        self.assertEqual(len(self.worker.business_calls), 1)
        self.assertEqual(self.worker.business_calls[0][1]["requested_at"], original_time)
        csv = self.outbox.pending()[0].attachment
        final_id = self.outbox.pending()[0].outbox_id
        await self.shutdown()
        self.now += 86_400
        self.open()
        await self.core.recover()
        await self.core.handle(message)
        self.assertEqual(len(self.worker.business_calls), 1)
        self.assertEqual(self.outbox.pending()[0].outbox_id, final_id)
        self.assertEqual(self.outbox.pending()[0].attachment, csv)
        self.assertEqual(self.docs.calls, [])

    async def test_followup_cannot_reuse_business_query_from_another_user_channel_or_generation(self) -> None:
        await self.core.handle(InboundMessage(50, 100, 200, REAL_REQUEST))
        self.acknowledge_all()
        self.now += 1
        await self.core.handle(InboundMessage(51, 101, 200, "en csv"))
        await self.core.handle(InboundMessage(52, 100, 201, "en csv"))
        await self.core.handle(InboundMessage(53, 100, 200, "olvida la conversación"))
        await self.core.handle(InboundMessage(54, 100, 200, "en csv"))
        self.assertEqual(len(self.worker.business_calls), 1)
        self.assertEqual(self.docs.calls, [])

    async def test_incomplete_worker_report_is_failed_and_never_becomes_followup_source(self) -> None:
        self.worker.report = {"state": "failed", "message": "No pude completar el conteo dentro del límite.", "complete": False}
        submission = await self.core.handle(InboundMessage(60, 100, 200, REAL_REQUEST))
        self.assertEqual(self.storage.get_inbound(submission.request_id).status, RequestStatus.FAILED)
        self.assertIn("No pude completar", self.outbox.pending()[0].content)
        self.now += 1
        await self.core.handle(InboundMessage(61, 100, 200, "en csv"))
        self.assertEqual(len(self.worker.business_calls), 1)
        self.assertEqual(self.storage.list_exchanges(KEY), [])

