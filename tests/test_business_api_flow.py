"""Business language through the real HTTP boundary to a read-only fake AWS CLI."""

from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

import aiohttp
from aiohttp.test_utils import TestServer

from app.memory import MemoryStore
from app.models import Backend, InboundMessage, Intent
from app.orchestrator import PooIAOrchestrator
from app.outbox import DurableOutbox
from app.storage import SQLiteStorage
from app.worker_client import WorkerClient
from tests.test_aws_csv_flow import ForbiddenModel
from tests.worker.test_api import FakeManager
from worker.api import create_app
from worker.aws_queries import AwsQueries
from worker.config import WorkerSettings
from worker.processes import CommandResult


class BusinessApiFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_request_reaches_count_http_and_csv_delivery_without_model(self) -> None:
        class ReadRunner:
            def __init__(self):
                self.calls = []
                self.pages = 0

            def run(self, argv, **kwargs):
                self.calls.append((tuple(argv), kwargs))
                if "--table-name=DealerConfig" in argv:
                    return CommandResult(0, json.dumps({"Items": [{
                        "dealer_id": {"S": "COMAZDCALC2"},
                        "time_zone": {"S": "America/Mexico_City"},
                    }]}))
                if "--table-name=Tasks" not in argv or "query" not in argv:
                    raise AssertionError("The business operation must query its configured tables")
                self.pages += 1
                if self.pages % 2:
                    return CommandResult(0, json.dumps({
                        "Count": 0, "ScannedCount": 0,
                        "LastEvaluatedKey": {
                            "id": {"S": "resume"}, "dealer_id": {"S": "COMAZDCALC2"},
                            "planned_start_at": {"S": "2026-09-07T08:00:00Z"},
                        },
                    }))
                return CommandResult(0, json.dumps({"Count": 17, "ScannedCount": 17}))

        runner = ReadRunner()
        settings = WorkerSettings(
            host="127.0.0.1", port=4097, username="test", password="synthetic-password",
            aws_enabled=True, aws_tasks_table="Tasks", aws_dealer_config_table="DealerConfig",
        )
        server = TestServer(create_app(settings, manager=FakeManager(),
                                      aws_queries=AwsQueries(settings, runner=runner)))
        await server.start_server()
        self.addAsyncCleanup(server.close)
        async with aiohttp.ClientSession() as session:
            worker = WorkerClient(session, base_url=str(server.make_url("")),
                                  username="test", password="synthetic-password")
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                storage = SQLiteStorage(root / "state.sqlite3")
                model = ForbiddenModel()
                core = PooIAOrchestrator(
                    storage=storage, memory=MemoryStore(storage), outbox=DurableOutbox(storage),
                    ollama=model, opencode=model, worker=worker, content_root=root, aws_enabled=True,
                )
                try:
                    anchor = datetime(2026, 9, 7, 1, 16, tzinfo=UTC).timestamp()
                    message = InboundMessage(9901, 100, 200,
                        "Puedes contar cuántas tareas tengo para el día de mañana en este dealer\n\n"
                        "COMAZDCALC2\nConsidera las tareas de asesor,técnico, mantenimiento .. etc",
                        received_at=anchor)
                    submission = await core.handle(message)
                    self.assertEqual(submission.intent, Intent.AWS_REPORT)
                    pending = core.pending_outputs(message.conversation_key)
                    final = [part for part in pending if storage.get_outbox(part.outbox_id).kind == "response"]
                    self.assertEqual(len(final), 1)
                    self.assertIn("17", final[0].content)
                    self.assertIn("2026-09-07", final[0].content)
                    self.assertIn("America/Mexico_City", final[0].content)

                    async def acknowledge(_part):
                        return "synthetic-delivery"

                    await core.flush_outputs(acknowledge, key=message.conversation_key)
                    followup = InboundMessage(9902, 100, 200, "dámelo en csv", received_at=anchor + 86400)
                    await core.handle(followup)
                    exports = [part for part in core.pending_outputs(message.conversation_key) if part.attachment]
                    self.assertEqual(len(exports), 1)
                    rows = list(csv.reader(io.StringIO(exports[0].attachment.data.decode("utf-8-sig"))))
                    self.assertEqual(len(rows), 2)
                    self.assertIn("2026-09-07", rows[1])
                    self.assertIn("17", rows[1])
                    self.assertEqual(storage.get_outbox(exports[0].outbox_id).backend, Backend.WORKER)
                    self.assertFalse(storage.get_outbox(exports[0].outbox_id).exchange_on_complete)
                    self.assertEqual(core.memory.snapshot(message.conversation_key).exchanges, ())
                    self.assertEqual(storage.list_jobs(), [])
                    self.assertEqual(model.calls, [])
                    self.assertEqual(len(runner.calls), 6)
                    for argv, _kwargs in runner.calls:
                        self.assertIn("query", argv)
                        self.assertNotIn("scan", argv)
                        self.assertNotIn("--filter-expression", argv)
                        if "--table-name=Tasks" in argv:
                            self.assertEqual(argv[argv.index("--select") + 1], "COUNT")
                finally:
                    await core.close()
                    storage.close()
