"""Offline coverage of CSV generation, transport, durable retry, and Discord upload."""

from __future__ import annotations

import base64
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import discord

from app.memory import MemoryStore
from app.models import Backend, CsvAttachment, InboundMessage, Intent, RequestStatus
from app.orchestrator import PooIAOrchestrator
from app.outbox import DurableOutbox
from app.storage import SQLiteStorage
from app.worker_client import WorkerClient
from tests.test_discord_delivery import AdapterClient
from tests.test_worker_client import FakeResponse
from tests.worker.test_aws_queries import FakeRunner
from worker.aws_queries import AwsQueries
from worker.config import WorkerSettings
from worker.processes import CommandResult


class OfflineWorkerSession:
    """Dispatch the client protocol directly to the real worker query service."""

    def __init__(self, service: AwsQueries) -> None:
        self.service = service
        self.calls = []
        self.reports = []

    def request(self, method, url, **kwargs):
        if (method, url) != ("POST", "http://offline-worker/v1/aws/query"):
            raise AssertionError(f"Unexpected worker operation: {method} {url}")
        payload = kwargs["json"]
        self.calls.append(dict(payload))
        report = self.service.query(
            payload["action"],
            table=payload.get("table"),
            log_group=payload.get("log_group"),
            output_format=payload.get("format", "text"),
        )
        self.reports.append(report)
        return FakeResponse(200, {"result": report})


class ForbiddenModel:
    def __init__(self) -> None:
        self.calls = []

    async def generate(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        raise AssertionError("CSV delivery must not call a model")

    research = generate


class InterruptedUploadChannel:
    id = 100

    def __init__(self) -> None:
        self.uploads = []
        self.streams = []

    async def send(self, content, *, file, allowed_mentions):
        if not isinstance(file, discord.File):
            raise AssertionError("The adapter must construct a real discord.File")
        self.uploads.append((content, file.filename, file.fp.read(), allowed_mentions.to_dict()))
        self.streams.append(file.fp)
        if len(self.uploads) == 1:
            raise ConnectionError("Synthetic interruption before delivery acknowledgement")
        return SimpleNamespace(id=902)


class AwsCsvFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_csv_retry_after_restart_reuses_original_bytes_without_aws_or_models(self) -> None:
        # This value exceeds the text report excerpt, contains CSV quoting and
        # Unicode, and must survive the entire structured export without loss.
        description = 'línea uno, "detalle"\n' + "ñ" * 1800
        runner = FakeRunner(CommandResult(0, json.dumps({"Items": [{
            "task_id": {"S": "task-1"},
            "task_available": {"BOOL": False},
            "description": {"S": description},
        }]})))
        service = AwsQueries(WorkerSettings(
            host="127.0.0.1", port=4097, username="worker",
            password="synthetic-password", aws_enabled=True,
        ), runner=runner)
        session = OfflineWorkerSession(service)
        worker = WorkerClient(
            session, base_url="http://offline-worker", username="worker",
            password="synthetic-password",
        )
        model = ForbiddenModel()
        channel = InterruptedUploadChannel()
        adapter = AdapterClient(channel)
        self.addAsyncCleanup(adapter.close)
        message = InboundMessage(
            960, 100, 200, "consulta registros de la tabla Tasks en DynamoDB en csv",
        )
        key = message.conversation_key

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "state.sqlite3"

            def reopen():
                storage = SQLiteStorage(database)
                orchestrator = PooIAOrchestrator(
                    storage=storage, memory=MemoryStore(storage),
                    outbox=DurableOutbox(storage), ollama=model, opencode=model,
                    worker=worker, content_root=root, aws_enabled=True,
                )
                return storage, orchestrator

            storage, orchestrator = reopen()
            try:
                submission = await orchestrator.handle(message)
                self.assertEqual(submission.intent, Intent.AWS_REPORT)
                pending = orchestrator.pending_outputs(key)
                self.assertEqual(len(pending), 1)
                original = pending[0]
                self.assertIsInstance(original.attachment, CsvAttachment)
                expected = base64.b64decode(session.reports[0]["attachment"]["content_base64"])
                self.assertEqual(original.attachment.data, expected)
                rows = list(csv.DictReader(io.StringIO(expected.decode("utf-8-sig"))))
                self.assertEqual(rows, [{
                    "description": description, "task_available": "false", "task_id": "task-1",
                }])
                persisted = storage.get_outbox(original.outbox_id)
                self.assertEqual(persisted.backend, Backend.WORKER)
                self.assertFalse(persisted.exchange_on_complete)

                with self.assertRaises(ConnectionError):
                    await orchestrator.flush_outputs(adapter._send_outbox_part, key=key)
                self.assertIsNone(orchestrator.pending_outputs(key)[0].acked_at)
            finally:
                await orchestrator.close()
                storage.close()

            storage, orchestrator = reopen()
            try:
                await orchestrator.recover()
                duplicate = await orchestrator.handle(message)
                self.assertFalse(duplicate.created)
                recovered = orchestrator.pending_outputs(key)
                self.assertEqual(len(recovered), 1)
                self.assertEqual(recovered[0].outbox_id, original.outbox_id)
                self.assertEqual(recovered[0].attachment, original.attachment)
                self.assertEqual(await orchestrator.flush_outputs(adapter._send_outbox_part, key=key), 1)
                self.assertEqual(orchestrator.pending_outputs(key), [])
                acknowledged = storage.list_outbox_parts(original.outbox_id)[0]
                self.assertEqual(acknowledged.discord_message_id, "902")
                self.assertIsNotNone(acknowledged.acked_at)
                self.assertEqual(storage.get_inbound(submission.request_id).status, RequestStatus.COMPLETED)
                self.assertEqual(orchestrator.memory.snapshot(key).exchanges, ())
                self.assertEqual(storage.list_jobs(), [])
            finally:
                await orchestrator.close()
                storage.close()

        self.assertEqual(session.calls, [{"action": "scan-dynamodb", "format": "csv", "table": "Tasks"}])
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(model.calls, [])
        self.assertEqual(len(channel.uploads), 2)
        self.assertEqual(channel.uploads[0], channel.uploads[1])
        self.assertEqual(channel.uploads[1][:3], (original.content, original.attachment.filename, expected))
        self.assertEqual(channel.uploads[1][3], {"parse": []})
        self.assertIsNot(channel.streams[0], channel.streams[1])
        self.assertTrue(all(stream.closed for stream in channel.streams))
