from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.models import Backend, ConversationKey, CsvAttachment, InboundMessage
from app.outbox import DurableOutbox
from app.storage import SQLiteStorage, StorageConflictError


KEY = ConversationKey("discord", 100, 200)


def register(storage: SQLiteStorage, message_id: int, text: str = "pregunta") -> str:
    return storage.register_inbound(
        InboundMessage(message_id, 100, 200, text, received_at=10)
    ).request.request_id


class OutboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.storage = SQLiteStorage(":memory:")
        self.outbox = DurableOutbox(self.storage)

    def tearDown(self) -> None:
        self.storage.close()

    def test_long_output_is_pre_split_to_discord_safe_parts(self) -> None:
        request_id = register(self.storage, 1)
        envelope = self.outbox.enqueue(
            request_id, "word " * 1_500, backend=Backend.OPENCODE
        )
        parts = self.outbox.parts(envelope.outbox_id)

        self.assertGreater(len(parts), 1)
        self.assertTrue(all(0 < len(part.content) <= 1_900 for part in parts))
        self.assertEqual(self.outbox.pending(KEY), parts)

    def test_generated_output_is_bounded_before_persisting_and_splitting(self) -> None:
        request_id = register(self.storage, 10)
        bounded = DurableOutbox(self.storage, max_output_chars=3_800)

        envelope = bounded.enqueue(request_id, "x" * 10_000)
        stored = self.storage.get_outbox(envelope.outbox_id)
        parts = bounded.parts(envelope.outbox_id)

        self.assertIsNotNone(stored)
        self.assertLessEqual(len(stored.assistant_text), 3_800)
        self.assertIn("recortada", stored.assistant_text)
        self.assertEqual(len(parts), 2)
        self.assertTrue(all(len(part.content) <= 1_900 for part in parts))

    def test_partial_send_does_not_create_exchange_final_ack_creates_one(self) -> None:
        request_id = register(self.storage, 2, "recuerda esto")
        envelope = self.outbox.enqueue(
            request_id, "x" * 4_000, backend=Backend.OLLAMA, now=20
        )
        parts = self.outbox.parts(envelope.outbox_id)

        self.outbox.acknowledge(parts[0], "discord-1", now=21)
        self.assertEqual(self.storage.list_exchanges(KEY), [])
        self.assertEqual(len(self.outbox.pending(KEY)), len(parts) - 1)

        for index, part in enumerate(parts[1:], start=2):
            self.outbox.acknowledge(part, f"discord-{index}", now=20 + index)

        exchanges = self.storage.list_exchanges(KEY)
        self.assertEqual(len(exchanges), 1)
        self.assertEqual(exchanges[0].user_text, "recuerda esto")
        self.assertEqual(exchanges[0].assistant_text, "x" * 4_000)
        self.assertEqual(self.outbox.pending(KEY), [])
        self.assertTrue(self.storage.get_outbox(envelope.outbox_id).is_complete)

    def test_ack_is_idempotent_and_never_duplicates_memory(self) -> None:
        request_id = register(self.storage, 3)
        envelope = self.outbox.enqueue(request_id, "respuesta")
        part = self.outbox.parts(envelope.outbox_id)[0]

        first = self.outbox.acknowledge(part, "discord-original", now=30)
        replay = self.outbox.acknowledge(part, "discord-replay", now=31)

        self.assertEqual(first.discord_message_id, "discord-original")
        self.assertEqual(replay.discord_message_id, "discord-original")
        self.assertEqual(len(self.storage.list_exchanges(KEY)), 1)

    def test_progress_is_delivered_but_never_added_to_memory(self) -> None:
        request_id = register(self.storage, 4)
        progress = self.outbox.enqueue_progress(
            request_id, "Estoy investigando…", dedupe_key="queued"
        )
        part = self.outbox.parts(progress.outbox_id)[0]
        self.outbox.acknowledge(part, "discord-progress")

        self.assertEqual(self.storage.list_exchanges(KEY), [])

    def test_latest_pending_progress_replaces_older_parts_but_keeps_receipt(self) -> None:
        request_id = register(self.storage, 601)
        receipt = self.outbox.enqueue(request_id, "Recibido.", kind="ack", dedupe_key="received")
        previous = self.outbox.enqueue_progress(request_id, "a" * 2_000, dedupe_key="started", now=20)
        previous_parts = self.outbox.parts(previous.outbox_id)
        self.outbox.acknowledge(previous_parts[0], "already-sent", now=21)
        latest = self.outbox.enqueue_progress(request_id, "Sigo esperando al worker.", dedupe_key="heartbeat-1", now=22)

        self.assertEqual({part.outbox_id for part in self.outbox.pending(KEY)}, {receipt.outbox_id, latest.outbox_id})
        self.assertEqual(self.storage.get_outbox(previous.outbox_id).completed_at, 22)
        preserved = self.outbox.parts(previous.outbox_id)
        self.assertEqual(preserved[0].discord_message_id, "already-sent")
        self.assertEqual(preserved[0].acked_at, 21)
        self.assertIsNone(preserved[1].discord_message_id)
        self.assertIsNone(preserved[1].acked_at)
        replay = self.outbox.enqueue_progress(request_id, "a" * 2_000, dedupe_key="started", now=23)
        self.assertTrue(replay.is_complete)
        self.assertFalse(self.storage.get_outbox(latest.outbox_id).is_complete)

    def test_final_output_suppresses_progress_but_preserves_receipt_and_other_requests(self) -> None:
        for kind in ("response", "error"):
            with self.subTest(kind=kind):
                request_id = register(self.storage, 602 if kind == "response" else 603)
                receipt = self.outbox.enqueue(request_id, "Recibido.", kind="ack", dedupe_key="received")
                progress = self.outbox.enqueue_progress(request_id, "Esperando.", dedupe_key="started")
                final = self.outbox.enqueue(request_id, "Resultado.", kind=kind, remember_exchange=False)
                late = self.outbox.enqueue_progress(request_id, "Todavía esperando.", dedupe_key="heartbeat-1")
                pending = {part.outbox_id for part in self.outbox.pending(KEY)}
                self.assertIn(receipt.outbox_id, pending)
                self.assertIn(final.outbox_id, pending)
                self.assertNotIn(progress.outbox_id, pending)
                self.assertNotIn(late.outbox_id, pending)
                self.assertTrue(self.storage.get_outbox(late.outbox_id).is_complete)
                self.assertIsNone(self.outbox.parts(late.outbox_id)[0].acked_at)

    def test_operational_output_cannot_enter_memory_even_with_default_flag(self) -> None:
        request_id = register(self.storage, 604)
        for kind in ("ack", "progress"):
            envelope = self.outbox.enqueue(request_id, kind, kind=kind, dedupe_key=kind)
            self.assertFalse(envelope.exchange_on_complete)
            self.outbox.acknowledge(self.outbox.parts(envelope.outbox_id)[0], f"sent-{kind}")
        self.assertEqual(self.storage.list_exchanges(KEY), [])
        final = self.outbox.enqueue(request_id, "Respuesta definitiva.")
        self.outbox.acknowledge(self.outbox.parts(final.outbox_id)[0], "sent-final")
        self.assertEqual([exchange.assistant_text for exchange in self.storage.list_exchanges(KEY)], ["Respuesta definitiva."])

    def test_duplicate_or_conflicting_final_does_not_change_pending_delivery(self) -> None:
        request_id = register(self.storage, 605)
        self.outbox.enqueue_progress(request_id, "Esperando.", dedupe_key="started")
        final = self.outbox.enqueue(request_id, "Resultado.")
        self.assertEqual(final, self.outbox.enqueue(request_id, "Resultado."))
        with self.assertRaises(StorageConflictError):
            self.outbox.enqueue(request_id, "Otro resultado.")
        self.assertEqual([part.outbox_id for part in self.outbox.pending(KEY)], [final.outbox_id])

    def test_response_started_before_forget_cannot_reenter_memory_after_final_ack(self) -> None:
        request_id = register(self.storage, 40, "old request")
        envelope = self.outbox.enqueue(request_id, "old response")
        self.storage.forget_conversation(KEY, now=20)

        part = self.outbox.parts(envelope.outbox_id)[0]
        self.outbox.acknowledge(part, "discord-after-forget", now=21)

        self.assertEqual(self.storage.list_exchanges(KEY), [])

    def test_enqueue_retry_is_idempotent_and_changed_content_conflicts(self) -> None:
        request_id = register(self.storage, 5)
        first = self.outbox.enqueue(request_id, "respuesta")
        same = self.outbox.enqueue(request_id, "respuesta")
        self.assertEqual(first, same)

        with self.assertRaises(StorageConflictError):
            self.outbox.enqueue(request_id, "respuesta diferente")

    def test_csv_is_persisted_once_on_first_part_and_never_enters_memory(self) -> None:
        request_id = register(self.storage, 501)
        csv = CsvAttachment("tasks.csv", b"task_id,task_available\n1,true\n")
        envelope = self.outbox.enqueue(
            request_id, "resultados " * 500, backend=Backend.WORKER, attachment=csv,
        )
        parts = self.outbox.parts(envelope.outbox_id)
        self.assertGreater(len(parts), 1)
        self.assertEqual(envelope.attachment, csv)
        self.assertEqual(self.storage.get_outbox(envelope.outbox_id).attachment, csv)
        self.assertFalse(envelope.exchange_on_complete)
        self.assertEqual(parts[0].attachment, csv)
        self.assertTrue(all(part.attachment is None for part in parts[1:]))
        acknowledged = self.outbox.acknowledge(parts[0], "csv-message")
        self.assertEqual(acknowledged.attachment, csv)
        self.assertTrue(all(part.attachment is None for part in self.outbox.pending(KEY)))
        for part in parts[1:]:
            self.outbox.acknowledge(part, f"text-{part.part_index}")
        self.assertEqual(self.storage.list_exchanges(KEY), [])

    def test_attachment_bytes_and_metadata_participate_in_deduplication(self) -> None:
        request_id = register(self.storage, 502)
        csv = CsvAttachment("tasks.csv", b"id\n1\n")
        first = self.outbox.enqueue(request_id, "CSV", attachment=csv)
        self.assertEqual(first, self.outbox.enqueue(request_id, "CSV", attachment=csv))
        for changed in (None, CsvAttachment("other.csv", csv.data), CsvAttachment(csv.filename, b"id\n2\n")):
            with self.subTest(changed=changed):
                with self.assertRaises(StorageConflictError):
                    self.outbox.enqueue(request_id, "CSV", attachment=changed, remember_exchange=False)

    def test_csv_value_object_rejects_unsafe_names_types_and_oversized_data(self) -> None:
        for name in ("../tasks.csv", "/tasks.csv", "tasks\\data.csv", "tasks.csv\n", "@everyone.csv", "tasks.exe"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    CsvAttachment(name, b"id\n1\n")
        with self.assertRaises(ValueError):
            CsvAttachment("tasks.csv", b"x" * (128 * 1024 + 1))
        with self.assertRaises(ValueError):
            CsvAttachment("tasks.csv", b"id\n", content_type="application/octet-stream")
        with self.assertRaises((TypeError, ValueError)):
            CsvAttachment("tasks.csv", "id\n")

    def test_pending_parts_follow_output_creation_then_part_order(self) -> None:
        first_request = register(self.storage, 6)
        second_request = register(self.storage, 7)
        first = self.outbox.enqueue_progress(
            first_request, "a" * 2_000, dedupe_key="first", now=20
        )
        second = self.outbox.enqueue_progress(
            second_request, "second", dedupe_key="second", now=21
        )

        pending = self.outbox.pending(KEY)
        self.assertEqual(
            [(part.outbox_id, part.part_index) for part in pending],
            [
                (first.outbox_id, 0),
                (first.outbox_id, 1),
                (second.outbox_id, 0),
            ],
        )


class OutboxPersistenceTests(unittest.TestCase):
    def test_restart_keeps_only_latest_progress_and_deduplicates_suppressed_phases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            first = SQLiteStorage(path)
            request_id = register(first, 606)
            outbox = DurableOutbox(first)
            receipt = outbox.enqueue(request_id, "Recibido.", kind="ack", dedupe_key="received")
            old = outbox.enqueue_progress(request_id, "Comencé.", dedupe_key="started")
            current = outbox.enqueue_progress(request_id, "En ejecución.", dedupe_key="heartbeat-1")
            first.close()

            second = SQLiteStorage(path)
            outbox = DurableOutbox(second)
            replay = outbox.enqueue_progress(request_id, "Comencé.", dedupe_key="started")
            self.assertEqual(replay.outbox_id, old.outbox_id)
            self.assertTrue(replay.is_complete)
            self.assertEqual([part.outbox_id for part in outbox.pending(KEY)], [receipt.outbox_id, current.outbox_id])
            outbox.enqueue(request_id, "Final.")
            second.close()

            third = SQLiteStorage(path)
            self.assertEqual([part.content for part in third.list_pending_outbox_parts(KEY)], ["Recibido.", "Final."])
            self.assertIsNone(third.list_outbox_parts(old.outbox_id)[0].acked_at)
            third.close()

    def test_restart_retains_csv_bytes_and_ack_prevents_attachment_resend(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "poo-ia.sqlite3"
            first = SQLiteStorage(path)
            request_id = register(first, 503)
            csv = CsvAttachment("tasks.csv", "id,nombre\n1,José\n".encode())
            output = DurableOutbox(first).enqueue(request_id, "x" * 2_100, attachment=csv)
            first.close()

            second = SQLiteStorage(path)
            outbox = DurableOutbox(second)
            pending = outbox.pending(KEY)
            self.assertEqual(pending[0].attachment, csv)
            self.assertEqual(pending[0].attachment.sha256, csv.sha256)
            outbox.acknowledge(pending[0], "file-and-first-text")
            second.close()

            third = SQLiteStorage(path)
            remaining = DurableOutbox(third).pending(KEY)
            self.assertEqual([part.part_index for part in remaining], [1])
            self.assertIsNone(remaining[0].attachment)
            self.assertFalse(third.get_outbox(output.outbox_id).is_complete)
            third.close()

    def test_reopen_resumes_only_unacknowledged_parts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "poo-ia.sqlite3"
            first_storage = SQLiteStorage(path)
            request_id = register(first_storage, 8)
            first_outbox = DurableOutbox(first_storage)
            envelope = first_outbox.enqueue(request_id, "z" * 4_000)
            parts = first_outbox.parts(envelope.outbox_id)
            first_outbox.acknowledge(parts[0], "discord-1")
            first_storage.close()

            second_storage = SQLiteStorage(path)
            second_outbox = DurableOutbox(second_storage)
            pending = second_outbox.pending(KEY)
            self.assertEqual(
                [part.part_index for part in pending],
                [part.part_index for part in parts[1:]],
            )
            for part in pending:
                second_outbox.acknowledge(part, f"discord-{part.part_index + 1}")
            self.assertEqual(len(second_storage.list_exchanges(KEY)), 1)
            second_storage.close()

    async def _unused(self) -> None:  # pragma: no cover - keeps unittest discovery simple
        pass


class OutboxFlushTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_progress_is_superseded_by_final_csv_without_losing_file(self) -> None:
        storage = SQLiteStorage(":memory:")
        self.addCleanup(storage.close)
        outbox = DurableOutbox(storage)
        request_id = register(storage, 608)
        progress = outbox.enqueue_progress(request_id, "Consultando AWS.", dedupe_key="aws-started")

        async def failing_sender(part):
            raise ConnectionError("Discord is temporarily unavailable")

        with self.assertRaises(ConnectionError):
            await outbox.flush(failing_sender, key=KEY)
        csv = CsvAttachment("tasks.csv", b"id\r\n1\r\n")
        final = outbox.enqueue(request_id, "Resultado CSV.", attachment=csv)
        sent = []

        async def sender(part):
            sent.append((part.outbox_id, part.content, part.attachment))
            return "file-and-caption"

        self.assertEqual(await outbox.flush(sender, key=KEY), 1)
        self.assertEqual(sent, [(final.outbox_id, "Resultado CSV.", csv)])
        self.assertIsNone(outbox.parts(progress.outbox_id)[0].acked_at)
        self.assertEqual(storage.list_exchanges(KEY), [])

    async def test_flush_skips_progress_superseded_while_sending_receipt(self) -> None:
        storage = SQLiteStorage(":memory:")
        self.addCleanup(storage.close)
        outbox = DurableOutbox(storage)
        request_id = register(storage, 607)
        outbox.enqueue(request_id, "Recibido.", kind="ack", dedupe_key="received")
        progress = outbox.enqueue_progress(request_id, "Esperando.", dedupe_key="started")
        sent = []

        async def sender(part):
            sent.append(part.content)
            if part.content == "Recibido.":
                outbox.enqueue(request_id, "Final.")
            return f"remote-{len(sent)}"

        self.assertEqual(await outbox.flush(sender, key=KEY), 1)
        self.assertEqual(sent, ["Recibido."])
        self.assertIsNone(outbox.parts(progress.outbox_id)[0].acked_at)
        self.assertEqual(await outbox.flush(sender, key=KEY), 1)
        self.assertEqual(sent, ["Recibido.", "Final."])

    async def test_csv_sender_failure_preserves_atomic_part_for_retry(self) -> None:
        storage = SQLiteStorage(":memory:")
        outbox = DurableOutbox(storage)
        request_id = register(storage, 504)
        csv = CsvAttachment("tasks.csv", b"id\n1\n")
        outbox.enqueue(request_id, "Resultado CSV", attachment=csv)
        attempts = []

        async def failing_sender(part):
            attempts.append((part.content, part.attachment))
            raise ConnectionError("Discord upload interrupted")

        with self.assertRaises(ConnectionError):
            await outbox.flush(failing_sender, key=KEY)
        self.assertEqual(outbox.pending(KEY)[0].attachment, csv)

        async def successful_sender(part):
            attempts.append((part.content, part.attachment))
            return "discord-file-message"

        self.assertEqual(await outbox.flush(successful_sender, key=KEY), 1)
        self.assertEqual(attempts, [("Resultado CSV", csv), ("Resultado CSV", csv)])
        self.assertEqual(storage.list_exchanges(KEY), [])
        storage.close()

    async def test_sender_failure_leaves_current_and_later_parts_pending(self) -> None:
        storage = SQLiteStorage(":memory:")
        outbox = DurableOutbox(storage)
        request_id = register(storage, 9)
        envelope = outbox.enqueue(request_id, "q" * 4_000)
        attempts = 0

        async def failing_sender(part):
            nonlocal attempts
            attempts += 1
            if attempts == 2:
                raise ConnectionError("simulated disconnect")
            return f"remote-{part.part_index}"

        with self.assertRaises(ConnectionError):
            await outbox.flush(failing_sender, key=KEY)

        self.assertEqual(
            [part.part_index for part in outbox.pending(KEY)], [1, 2]
        )
        self.assertEqual(storage.list_exchanges(KEY), [])

        async def successful_sender(part):
            return f"retry-{part.part_index}"

        self.assertEqual(await outbox.flush(successful_sender, key=KEY), 2)
        self.assertEqual(len(storage.list_exchanges(KEY)), 1)
        self.assertTrue(storage.get_outbox(envelope.outbox_id).is_complete)
        storage.close()
