from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.models import (
    Backend,
    ConversationKey,
    CsvAttachment,
    InboundMessage,
    Intent,
    JobKind,
    JobStatus,
    RequestStatus,
)
from app.storage import (
    InvalidJobTransition,
    SQLiteStorage,
    StorageConflictError,
    StorageError,
    deterministic_job_id,
    deterministic_request_id,
)


def message(
    message_id: int,
    text: str = "consulta",
    *,
    channel_id: int = 100,
    user_id: int = 200,
    received_at: float | None = None,
) -> InboundMessage:
    return InboundMessage(
        message_id=message_id,
        channel_id=channel_id,
        user_id=user_id,
        text=text,
        received_at=received_at,
    )


class StorageMigrationTests(unittest.TestCase):
    def test_version_three_outbox_survives_attachment_migration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            storage = SQLiteStorage(path)
            request = storage.register_inbound(message(601)).request
            output = storage.create_outbox(
                request.request_id, kind="response", dedupe_key="final", assistant_text="text-only",
                parts=("text-only",), backend=Backend.WORKER, exchange_on_complete=False,
            )
            storage.close()
            with sqlite3.connect(path) as connection:
                connection.execute("DROP TABLE outbox_attachments")
                connection.execute("DELETE FROM schema_migrations WHERE version = 4")
            upgraded = SQLiteStorage(path)
            self.assertEqual(upgraded.schema_version, 4)
            self.assertIsNone(upgraded.get_outbox(output.outbox_id).attachment)
            part = upgraded.list_pending_outbox_parts()[0]
            self.assertEqual(part.content, "text-only")
            self.assertIsNone(part.attachment)
            upgraded.acknowledge_outbox_part(part.outbox_id, part.part_index, "existing-text")
            upgraded.close()

    def test_migration_is_idempotent_and_file_data_survives_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "poo-ia.sqlite3"
            first = SQLiteStorage(path)
            registration = first.register_inbound(
                message(1), intent=Intent.RESEARCH, backend=Backend.OPENCODE
            )
            self.assertEqual(first.schema_version, 4)
            self.assertEqual(first.journal_mode, "wal")
            self.assertTrue(first.foreign_keys_enabled)
            first.close()

            second = SQLiteStorage(path)
            self.assertEqual(second.schema_version, 4)
            self.assertEqual(
                second.get_inbound(registration.request.request_id), registration.request
            )
            second.close()

    def test_initialization_failure_is_not_silently_downgraded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory_path = Path(temporary_directory) / "database-directory"
            directory_path.mkdir()
            with self.assertRaises(Exception):
                SQLiteStorage(directory_path)

    def test_execution_context_migration_clears_brain_and_preserves_service(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            storage = SQLiteStorage(path)
            brain_key = ConversationKey("discord", 100, 200)
            service_key = ConversationKey("discord", 101, 200)
            storage.set_conversation_context(brain_key, active_repository="brain-capnet", last_job_id="old-brain")
            storage.set_conversation_context(service_key, active_repository="capnet-next-lambda-tasks", last_job_id="service-job")
            storage.close()
            with sqlite3.connect(path) as connection:
                connection.execute("DELETE FROM schema_migrations WHERE version = 3")
            upgraded = SQLiteStorage(path)
            self.assertIsNone(upgraded.get_conversation(brain_key).active_repository)
            self.assertIsNone(upgraded.get_conversation(brain_key).last_job_id)
            self.assertEqual(upgraded.get_conversation(service_key).active_repository, "capnet-next-lambda-tasks")
            self.assertEqual(upgraded.get_conversation(service_key).last_job_id, "service-job")
            upgraded.close()

    def test_existing_version_one_database_receives_notification_ack_migration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "poo-ia.sqlite3"
            migration = (
                Path(__file__).parent.parent / "app" / "migrations" / "001_initial.sql"
            )
            script = migration.read_text(encoding="utf-8")
            checksum = hashlib.sha256(migration.read_bytes()).hexdigest()
            connection = sqlite3.connect(path)
            connection.execute(
                """
                CREATE TABLE schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    checksum TEXT NOT NULL,
                    applied_at REAL NOT NULL
                )
                """
            )
            connection.executescript(script)
            connection.execute(
                """
                INSERT INTO conversations(
                    id, source, channel_id, user_id, created_at, updated_at
                ) VALUES (1, 'discord', '100', '200', 1, 1)
                """
            )
            connection.execute(
                """
                INSERT INTO inbound_requests(
                    request_id, source, external_message_id, conversation_id,
                    content, status, memory_generation, created_at, updated_at
                ) VALUES ('request-v1', 'discord', 'v1', 1, 'consulta',
                          'completed', 0, 1, 1)
                """
            )
            connection.execute(
                """
                INSERT INTO jobs(
                    job_id, request_id, kind, state, payload_json,
                    checkpoint_json, attempts, created_at, updated_at, finished_at
                ) VALUES ('job-v1', 'request-v1', 'research', 'succeeded', '{}',
                          '{}', 1, 1, 2, 2)
                """
            )
            connection.execute(
                """
                INSERT INTO outbox(
                    outbox_id, conversation_id, request_id, kind, dedupe_key,
                    assistant_text, exchange_on_complete, created_at, completed_at
                ) VALUES ('outbox-v1', 1, 'request-v1', 'response', 'final',
                          'resultado', 1, 3, 4)
                """
            )
            connection.execute(
                """
                INSERT INTO schema_migrations(version, name, checksum, applied_at)
                VALUES (1, ?, ?, 1)
                """,
                (migration.name, checksum),
            )
            connection.commit()
            connection.close()

            storage = SQLiteStorage(path)
            self.assertEqual(storage.schema_version, 4)
            self.assertEqual(
                storage.get_job("job-v1").notification_completed_at, 3
            )
            job = storage.register_inbound(
                message(2), job_kind=JobKind.RESEARCH
            ).job
            self.assertIsNone(job.notification_completed_at)
            storage.close()

    def test_rejects_unknown_future_schema_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "poo-ia.sqlite3"
            storage = SQLiteStorage(path)
            storage.close()
            connection = sqlite3.connect(path)
            connection.execute(
                "INSERT INTO schema_migrations(version, name, checksum, applied_at) "
                "VALUES (999, '999_future.sql', 'future', 1)"
            )
            connection.commit()
            connection.close()

            with self.assertRaisesRegex(Exception, "newer than this application"):
                SQLiteStorage(path)

    def test_rejects_changed_applied_migration_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "poo-ia.sqlite3"
            storage = SQLiteStorage(path)
            storage.close()
            connection = sqlite3.connect(path)
            connection.execute(
                "UPDATE schema_migrations SET checksum = 'tampered' WHERE version = 1"
            )
            connection.commit()
            connection.close()

            with self.assertRaisesRegex(Exception, "checksum"):
                SQLiteStorage(path)


class InboundAndJobStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.storage = SQLiteStorage(":memory:", clock=lambda: 100.0)

    def tearDown(self) -> None:
        self.storage.close()

    def test_request_and_optional_job_are_atomic_and_deterministic(self) -> None:
        registration = self.storage.register_inbound(
            message(44, "agrega task_available"),
            intent=Intent.CODE_CHANGE,
            backend=Backend.WORKER,
            job_kind=JobKind.CODEX,
            repository="capnet-next-lambda-tasks",
            payload={"request": "agrega task_available"},
        )

        expected_request_id = deterministic_request_id("discord", 44)
        self.assertEqual(registration.request.request_id, expected_request_id)
        self.assertEqual(
            registration.job.job_id,
            deterministic_job_id(expected_request_id, JobKind.CODEX),
        )
        self.assertEqual(registration.job.state, JobStatus.QUEUED)
        self.assertEqual(registration.conversation.last_job_id, registration.job.job_id)
        self.assertEqual(
            registration.conversation.active_repository,
            "capnet-next-lambda-tasks",
        )
        self.assertEqual(len(self.storage.list_job_events(registration.job.job_id)), 1)

    def test_duplicate_discord_event_never_creates_a_second_job(self) -> None:
        first = self.storage.register_inbound(
            message(45), job_kind=JobKind.RESEARCH, payload={"question": "a"}
        )
        duplicate = self.storage.register_inbound(
            message(45, "content changed in a replay"),
            job_kind=JobKind.CODEX,
            payload={"different": True},
        )

        self.assertTrue(first.created)
        self.assertFalse(duplicate.created)
        self.assertEqual(duplicate.request, first.request)
        self.assertEqual(duplicate.job, first.job)
        self.assertEqual(len(self.storage.list_jobs()), 1)

    def test_ensure_job_is_idempotent_and_rejects_changed_payload(self) -> None:
        registration = self.storage.register_inbound(message(46))
        first = self.storage.ensure_job_for_request(
            registration.request.request_id,
            JobKind.CODEX,
            repository="repo-a",
            payload={"task": "x"},
        )
        same = self.storage.ensure_job_for_request(
            registration.request.request_id,
            JobKind.CODEX,
            repository="repo-a",
            payload={"task": "x"},
        )
        self.assertEqual(same, first)

        with self.assertRaises(StorageConflictError):
            self.storage.ensure_job_for_request(
                registration.request.request_id,
                JobKind.CODEX,
                repository="repo-a",
                payload={"task": "different"},
            )

    def test_valid_transitions_are_recorded_and_invalid_transition_rolls_back(self) -> None:
        job = self.storage.register_inbound(
            message(47), job_kind=JobKind.CODEX
        ).job
        running = self.storage.transition_job(
            job.job_id, JobStatus.RUNNING, message="Codex iniciado"
        )
        prepared = self.storage.transition_job(
            job.job_id,
            JobStatus.PREPARED,
            branch="poo-ia/job-task",
            checkpoint={"diff": "ready"},
        )

        self.assertEqual(running.attempts, 1)
        self.assertEqual(prepared.branch, "poo-ia/job-task")
        self.assertEqual(prepared.checkpoint, {"diff": "ready"})
        event_count = len(self.storage.list_job_events(job.job_id))
        with self.assertRaises(InvalidJobTransition):
            self.storage.transition_job(job.job_id, JobStatus.RUNNING)
        self.assertEqual(self.storage.get_job(job.job_id), prepared)
        self.assertEqual(len(self.storage.list_job_events(job.job_id)), event_count)

    def test_fifo_and_terminal_state(self) -> None:
        first = self.storage.register_inbound(
            message(48, received_at=10), job_kind=JobKind.RESEARCH
        ).job
        second = self.storage.register_inbound(
            message(49, received_at=20), job_kind=JobKind.CODEX
        ).job
        self.assertEqual(self.storage.next_queued_job().job_id, first.job_id)

        self.storage.transition_job(first.job_id, JobStatus.RUNNING, now=30)
        completed = self.storage.transition_job(
            first.job_id, JobStatus.SUCCEEDED, summary="listo", now=40
        )
        self.assertTrue(completed.state.is_terminal)
        self.assertEqual(completed.finished_at, 40)
        self.assertEqual(self.storage.next_queued_job().job_id, second.job_id)

    def test_lists_only_direct_requests_missing_durable_output_for_recovery(self) -> None:
        recoverable = self.storage.register_inbound(
            message(80), intent=Intent.CHAT, backend=Backend.OLLAMA
        ).request
        completed = self.storage.register_inbound(
            message(81), intent=Intent.AWS_REPORT, backend=Backend.NONE
        ).request
        self.storage.update_request_status(completed.request_id, RequestStatus.COMPLETED)
        with_output = self.storage.register_inbound(
            message(82), intent=Intent.CHAT, backend=Backend.OLLAMA
        ).request
        self.storage.create_outbox(
            with_output.request_id,
            kind="response",
            dedupe_key="final",
            assistant_text="ya existe",
            parts=("ya existe",),
            backend=Backend.OLLAMA,
            exchange_on_complete=True,
        )
        self.storage.register_inbound(
            message(83),
            intent=Intent.RESEARCH,
            backend=Backend.OPENCODE,
            job_kind=JobKind.RESEARCH,
        )

        messages = self.storage.list_recoverable_direct_messages()

        self.assertEqual([str(item.message_id) for item in messages], ["80"])
        self.assertEqual(str(messages[0].channel_id), "100")
        self.assertEqual(str(messages[0].user_id), "200")
        self.assertEqual(
            self.storage.get_inbound(recoverable.request_id).status,
            RequestStatus.RECEIVED,
        )

    def test_final_notification_ack_is_idempotent_and_resets_for_publication(self) -> None:
        job = self.storage.register_inbound(
            message(54), job_kind=JobKind.CODEX
        ).job
        with self.assertRaises(InvalidJobTransition):
            self.storage.mark_job_notification_completed(job.job_id)

        self.storage.transition_job(job.job_id, JobStatus.RUNNING, now=101)
        prepared = self.storage.transition_job(
            job.job_id, JobStatus.PREPARED, now=102
        )
        self.assertIsNone(prepared.notification_completed_at)
        self.assertEqual(
            [item.job_id for item in self.storage.list_jobs_pending_notification()],
            [job.job_id],
        )

        acknowledged = self.storage.mark_job_notification_completed(
            job.job_id, now=103
        )
        replay = self.storage.mark_job_notification_completed(job.job_id, now=104)
        self.assertEqual(acknowledged.notification_completed_at, 103)
        self.assertEqual(replay.notification_completed_at, 103)
        self.assertEqual(self.storage.list_jobs_pending_notification(), [])

        publishing = self.storage.transition_job(
            job.job_id, JobStatus.PUBLISHING, now=105
        )
        self.assertIsNone(publishing.notification_completed_at)
        self.assertEqual(self.storage.list_jobs_pending_notification(), [])
        succeeded = self.storage.transition_job(
            job.job_id, JobStatus.SUCCEEDED, now=106
        )
        self.assertIsNone(succeeded.notification_completed_at)
        self.assertEqual(
            [item.job_id for item in self.storage.list_jobs_pending_notification()],
            [job.job_id],
        )

    def test_publication_wrapper_can_preserve_existing_target_notification_ack(self) -> None:
        job = self.storage.register_inbound(
            message(55), job_kind=JobKind.CODEX
        ).job
        self.storage.transition_job(job.job_id, JobStatus.RUNNING, now=101)
        self.storage.transition_job(job.job_id, JobStatus.PREPARED, now=102)
        self.storage.mark_job_notification_completed(job.job_id, now=103)

        publishing = self.storage.transition_job(
            job.job_id,
            JobStatus.PUBLISHING,
            preserve_notification_ack=True,
            now=104,
        )
        succeeded = self.storage.transition_job(
            job.job_id,
            JobStatus.SUCCEEDED,
            preserve_notification_ack=True,
            now=105,
        )

        self.assertEqual(publishing.notification_completed_at, 103)
        self.assertEqual(succeeded.notification_completed_at, 103)
        self.assertEqual(self.storage.list_jobs_pending_notification(), [])

    def test_cleanup_can_remove_remote_session_checkpoint_from_terminal_job(self) -> None:
        job = self.storage.register_inbound(
            message(56), job_kind=JobKind.RESEARCH
        ).job
        self.storage.transition_job(job.job_id, JobStatus.RUNNING)
        self.storage.update_running_job_checkpoint(
            job.job_id,
            {"opencode_session_id": "orphan", "result_path": "keep"},
        )
        self.storage.transition_job(job.job_id, JobStatus.FAILED)

        cleaned = self.storage.remove_job_checkpoint_keys(
            job.job_id, ("opencode_session_id",)
        )

        self.assertEqual(cleaned.state, JobStatus.FAILED)
        self.assertEqual(cleaned.checkpoint, {"result_path": "keep"})

    def test_operational_prune_keeps_nonterminal_jobs(self) -> None:
        terminal = self.storage.register_inbound(
            message(50, received_at=1), job_kind=JobKind.RESEARCH
        ).job
        pending = self.storage.register_inbound(
            message(51, received_at=1), job_kind=JobKind.CODEX
        ).job
        self.storage.transition_job(terminal.job_id, JobStatus.RUNNING, now=2)
        self.storage.transition_job(terminal.job_id, JobStatus.SUCCEEDED, now=3)
        self.storage.mark_job_notification_completed(terminal.job_id, now=4)

        _, deleted_requests = self.storage.prune(
            memory_retention_seconds=7 * 86_400,
            operational_retention_seconds=30 * 86_400,
            now=31 * 86_400,
        )

        self.assertEqual(deleted_requests, 1)
        self.assertIsNone(self.storage.get_job(terminal.job_id))
        self.assertEqual(self.storage.get_job(pending.job_id).state, JobStatus.QUEUED)

    def test_operational_prune_keeps_terminal_job_with_pending_notification(self) -> None:
        job = self.storage.register_inbound(
            message(55, received_at=1), job_kind=JobKind.RESEARCH
        ).job
        self.storage.transition_job(job.job_id, JobStatus.RUNNING, now=2)
        self.storage.transition_job(job.job_id, JobStatus.SUCCEEDED, now=3)

        _, deleted_requests = self.storage.prune(
            memory_retention_seconds=7 * 86_400,
            operational_retention_seconds=30 * 86_400,
            now=31 * 86_400,
        )

        self.assertEqual(deleted_requests, 0)
        retained = self.storage.get_job(job.job_id)
        self.assertIsNotNone(retained)
        self.assertIsNone(retained.notification_completed_at)

    def test_operational_prune_keeps_remote_session_owner_until_cleanup(self) -> None:
        job = self.storage.register_inbound(
            message(57, received_at=1), job_kind=JobKind.RESEARCH
        ).job
        self.storage.transition_job(job.job_id, JobStatus.RUNNING, now=2)
        self.storage.update_running_job_checkpoint(
            job.job_id, {"opencode_session_id": "still-remote"}, now=3
        )
        self.storage.transition_job(job.job_id, JobStatus.FAILED, now=4)
        self.storage.mark_job_notification_completed(job.job_id, now=5)

        _, deleted_requests = self.storage.prune(
            memory_retention_seconds=7 * 86_400,
            operational_retention_seconds=30 * 86_400,
            now=31 * 86_400,
        )

        self.assertEqual(deleted_requests, 0)
        self.assertEqual(
            self.storage.get_job(job.job_id).checkpoint,
            {"opencode_session_id": "still-remote"},
        )

        self.storage.remove_job_checkpoint_keys(
            job.job_id, ("opencode_session_id",), now=31 * 86_400 + 1
        )
        _, deleted_requests = self.storage.prune(
            memory_retention_seconds=7 * 86_400,
            operational_retention_seconds=30 * 86_400,
            now=31 * 86_400 + 2,
        )
        self.assertEqual(deleted_requests, 1)
        self.assertIsNone(self.storage.get_job(job.job_id))

    def test_operational_prune_keeps_unacknowledged_discord_output(self) -> None:
        registration = self.storage.register_inbound(
            message(56, received_at=1), job_kind=JobKind.RESEARCH
        )
        job = registration.job
        self.storage.transition_job(job.job_id, JobStatus.RUNNING, now=2)
        self.storage.transition_job(job.job_id, JobStatus.SUCCEEDED, now=3)
        self.storage.mark_job_notification_completed(job.job_id, now=4)
        output = self.storage.create_outbox(
            registration.request.request_id,
            kind="response",
            dedupe_key="final",
            assistant_text="resultado pendiente",
            parts=("resultado pendiente",),
            backend=Backend.OPENCODE,
            exchange_on_complete=True,
            now=5,
        )

        _, deleted_requests = self.storage.prune(
            memory_retention_seconds=7 * 86_400,
            operational_retention_seconds=30 * 86_400,
            now=31 * 86_400,
        )

        self.assertEqual(deleted_requests, 0)
        self.assertIsNotNone(self.storage.get_job(job.job_id))
        self.assertEqual(
            self.storage.get_conversation(
                ConversationKey("discord", 100, 200)
            ).last_job_id,
            job.job_id,
        )

        self.storage.acknowledge_outbox_part(
            output.outbox_id, 0, "discord-56", now=31 * 86_400 + 1
        )
        _, deleted_requests = self.storage.prune(
            memory_retention_seconds=7 * 86_400,
            operational_retention_seconds=30 * 86_400,
            now=31 * 86_400 + 2,
        )
        self.assertEqual(deleted_requests, 1)
        self.assertIsNone(self.storage.get_job(job.job_id))

    def test_request_status_can_be_persisted(self) -> None:
        request = self.storage.register_inbound(message(52)).request
        updated = self.storage.update_request_status(
            request.request_id, RequestStatus.PROCESSING, now=101
        )
        self.assertEqual(updated.status, RequestStatus.PROCESSING)
        self.assertEqual(updated.updated_at, 101)

    def test_conversation_context_can_be_updated_from_neutral_request_id(self) -> None:
        request = self.storage.register_inbound(message(53)).request

        updated = self.storage.set_conversation_context_for_request(
            request.request_id, active_repository="capnet-next-lambda-tasks", now=102
        )

        self.assertEqual(updated.active_repository, "capnet-next-lambda-tasks")
        self.assertEqual(
            self.storage.get_conversation_for_request(request.request_id), updated
        )
        with self.assertRaises(KeyError):
            self.storage.set_conversation_context_for_request(
                "missing-request", active_repository="anything"
            )


class AttachmentStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.storage = SQLiteStorage(":memory:")
        self.request = self.storage.register_inbound(message(610, received_at=1)).request
        self.csv = CsvAttachment("tasks.csv", b"id,available\n1,true\n")

    def tearDown(self) -> None:
        self.storage.close()

    def enqueue(self):
        return self.storage.create_outbox(
            self.request.request_id, kind="response", dedupe_key="final", assistant_text="CSV listo",
            parts=("CSV listo",), backend=Backend.WORKER, exchange_on_complete=True,
            attachment=self.csv, now=2,
        )

    def test_attachment_is_an_integrity_checked_blob(self) -> None:
        output = self.enqueue()
        row = self.storage._connection.execute(
            "SELECT typeof(data), sha256, content_type FROM outbox_attachments WHERE outbox_id = ?",
            (output.outbox_id,),
        ).fetchone()
        self.assertEqual(tuple(row), ("blob", self.csv.sha256, "text/csv"))
        self.storage._connection.execute(
            "UPDATE outbox_attachments SET data = ? WHERE outbox_id = ?",
            (b"tampered\n", output.outbox_id),
        )
        with self.assertRaisesRegex(StorageError, "integrity"):
            self.storage.get_outbox(output.outbox_id)
        with self.assertRaisesRegex(StorageError, "integrity"):
            self.storage.list_pending_outbox_parts()

    def test_attachment_insert_failure_rolls_back_envelope_and_parts(self) -> None:
        self.storage._connection.executescript(
            "CREATE TRIGGER reject_attachment BEFORE INSERT ON outbox_attachments "
            "BEGIN SELECT RAISE(ABORT, 'simulated attachment failure'); END;"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.enqueue()
        for table in ("outbox", "outbox_parts", "outbox_attachments"):
            self.assertEqual(self.storage._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    def test_retention_keeps_pending_blob_and_cascades_after_ack(self) -> None:
        output = self.enqueue()
        self.storage.update_request_status(self.request.request_id, RequestStatus.COMPLETED, now=3)
        self.storage.prune(
            memory_retention_seconds=7 * 86_400, operational_retention_seconds=30 * 86_400,
            now=31 * 86_400,
        )
        self.assertEqual(self.storage.get_outbox(output.outbox_id).attachment, self.csv)
        self.storage.acknowledge_outbox_part(output.outbox_id, 0, "file-message", now=31 * 86_400 + 1)
        self.storage.prune(
            memory_retention_seconds=7 * 86_400, operational_retention_seconds=30 * 86_400,
            now=31 * 86_400 + 2,
        )
        self.assertIsNone(self.storage.get_outbox(output.outbox_id))
        self.assertEqual(self.storage._connection.execute("SELECT COUNT(*) FROM outbox_attachments").fetchone()[0], 0)
        self.assertEqual(self.storage.list_exchanges(ConversationKey("discord", 100, 200)), [])


class AwsRequestHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.storage = SQLiteStorage(":memory:")
        self.key = ConversationKey("discord", 100, 200)

    def tearDown(self) -> None:
        self.storage.close()

    def completed_query(self, message_id, timestamp, *, status=RequestStatus.COMPLETED,
                        output_backend=Backend.WORKER, user_id=200):
        request = self.storage.register_inbound(
            message(message_id, f"consulta AWS {message_id}", user_id=user_id, received_at=timestamp),
            intent=Intent.AWS_REPORT, backend=Backend.WORKER,
        ).request
        self.storage.create_outbox(
            request.request_id, kind="response", dedupe_key="final", assistant_text="reporte",
            parts=("reporte",), backend=output_backend, exchange_on_complete=False, now=timestamp,
        )
        return self.storage.update_request_status(request.request_id, status, now=timestamp)

    def test_history_is_scoped_successful_and_anchored_before_current_request(self) -> None:
        earlier = self.completed_query(620, 10)
        self.completed_query(621, 15, status=RequestStatus.FAILED)
        self.completed_query(622, 20, output_backend=Backend.STORAGE)
        self.completed_query(623, 25, user_id=999)
        same_time_earlier = self.completed_query(624, 30)
        current = self.storage.register_inbound(message(625, "damelo en csv", received_at=30)).request
        self.completed_query(626, 30)
        self.completed_query(627, 40)

        recent = self.storage.recent_aws_query_requests(self.key, exclude_request_id=current.request_id)
        self.assertEqual(recent, [same_time_earlier, earlier])
        self.assertEqual(
            self.storage.latest_aws_query_request(self.key, exclude_request_id=current.request_id),
            same_time_earlier,
        )
        self.assertEqual(self.storage.list_exchanges(self.key), [])
        self.assertEqual(self.storage.recent_aws_query_requests(self.key, exclude_request_id="missing"), [])

    def test_forget_prevents_reusing_older_operational_query_as_context(self) -> None:
        self.completed_query(628, 10)
        self.storage.forget_conversation(self.key, now=20)
        current = self.storage.register_inbound(message(629, "damelo en csv", received_at=30)).request
        self.assertEqual(self.storage.recent_aws_query_requests(self.key, exclude_request_id=current.request_id), [])
