"""Durable SQLite storage for conversations, jobs, and Discord delivery.

``SQLiteStorage`` owns one connection and serializes every transaction with a
re-entrant lock.  This is intentional: Poo-IA has one core process and one
SQLite writer, while asynchronous orchestration is serialized per conversation
in :mod:`app.memory`.  No caller is allowed to hold a cursor across an await.
"""

from __future__ import annotations

import json
import hashlib
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .models import (
    Backend,
    Conversation,
    ConversationKey,
    CsvAttachment,
    Exchange,
    InboundMessage,
    InboundRegistration,
    InboundRequest,
    Intent,
    Job,
    JobEvent,
    JobKind,
    JobStatus,
    OutboxMessage,
    OutboxPart,
    RequestStatus,
    ValidationStatus,
)


REQUEST_NAMESPACE = uuid.UUID("0e3980f8-1215-4ea6-a5a0-69e956838fee")
JOB_NAMESPACE = uuid.UUID("f0b2b298-b5ee-4265-8212-9b262436c647")
OUTBOX_NAMESPACE = uuid.UUID("acbaf8ac-e97c-4aa3-bd41-954ff732f20d")


class StorageError(RuntimeError):
    """Base error for safe storage failures."""


class StorageConflictError(StorageError):
    """Raised when an idempotency key is reused with different content."""


class InvalidJobTransition(StorageError):
    """Raised when a caller attempts a transition outside the state machine."""


_UNSET = object()
_CHECKPOINT_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_ATTACHMENT_COLUMNS = (
    "a.filename AS attachment_filename, a.content_type AS attachment_content_type, "
    "a.data AS attachment_data, a.sha256 AS attachment_sha256"
)

_ALLOWED_JOB_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.QUEUED: frozenset({JobStatus.RUNNING, JobStatus.CANCELLED}),
    JobStatus.RUNNING: frozenset(
        {
            JobStatus.PREPARED,
            JobStatus.SUCCEEDED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }
    ),
    JobStatus.PREPARED: frozenset(
        {JobStatus.PUBLISHING, JobStatus.FAILED, JobStatus.CANCELLED}
    ),
    JobStatus.PUBLISHING: frozenset(
        {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}
    ),
    JobStatus.SUCCEEDED: frozenset(),
    JobStatus.FAILED: frozenset(),
    JobStatus.CANCELLED: frozenset(),
}

_NOTIFIABLE_JOB_STATES = (
    JobStatus.PREPARED,
    JobStatus.SUCCEEDED,
    JobStatus.FAILED,
    JobStatus.CANCELLED,
)


def _enum_value(value: str | EnumLike | None) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "value", value))


class EnumLike:
    """Structural documentation helper for enum-like values."""

    value: str


def _json_object(value: Mapping[str, Any] | None) -> str:
    try:
        return json.dumps(value or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        raise ValueError("job payloads and checkpoints must be JSON objects") from error


def _load_json_object(raw: str) -> Mapping[str, Any]:
    loaded = json.loads(raw)
    if not isinstance(loaded, dict):
        raise StorageError("stored job JSON is not an object")
    return loaded


def deterministic_request_id(source: str, external_message_id: str | int) -> str:
    """Return the stable request UUID for an external transport event."""
    return str(uuid.uuid5(REQUEST_NAMESPACE, f"{source}:{external_message_id}"))


def deterministic_job_id(request_id: str, kind: JobKind | str) -> str:
    """Return the stable job UUID for one request and job kind."""
    return str(uuid.uuid5(JOB_NAMESPACE, f"{request_id}:{_enum_value(kind)}"))


def deterministic_outbox_id(request_id: str, kind: str, dedupe_key: str) -> str:
    """Return a stable output UUID so enqueue retries cannot duplicate output."""
    return str(uuid.uuid5(OUTBOX_NAMESPACE, f"{request_id}:{kind}:{dedupe_key}"))


class SQLiteStorage:
    """The core's single, durable SQLite writer."""

    def __init__(
        self,
        path: str | Path,
        *,
        busy_timeout_ms: int = 5_000,
        clock: callable = time.time,
    ) -> None:
        if busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms must be positive")

        self.path = Path(path) if str(path) != ":memory:" else Path(":memory:")
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._lock = threading.RLock()
        self._closed = False
        try:
            self._connection = sqlite3.connect(
                str(path),
                isolation_level=None,
                check_same_thread=False,
                timeout=busy_timeout_ms / 1_000,
            )
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._apply_migrations()
        except (sqlite3.Error, StorageError) as error:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            detail = " ".join(str(error).split())[:500]
            suffix = f": {detail}" if detail else ""
            raise StorageError(
                f"could not initialize SQLite storage at {path}{suffix}"
            ) from error

    def __enter__(self) -> "SQLiteStorage":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @property
    def journal_mode(self) -> str:
        with self._lock:
            return str(self._connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()

    @property
    def foreign_keys_enabled(self) -> bool:
        with self._lock:
            return bool(self._connection.execute("PRAGMA foreign_keys").fetchone()[0])

    @property
    def schema_version(self) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
            ).fetchone()
            return int(row[0])

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True

    def _apply_migrations(self) -> None:
        migrations_root = Path(__file__).with_name("migrations")
        migration_files = sorted(migrations_root.glob("[0-9][0-9][0-9]_*.sql"))
        if not migration_files:
            raise StorageError("no SQLite migrations were found")

        known: dict[int, tuple[Path, str]] = {}
        for migration_path in migration_files:
            version_text, _, _name = migration_path.name.partition("_")
            version = int(version_text)
            if version in known:
                raise StorageError(f"duplicate SQLite migration version {version}")
            checksum = hashlib.sha256(migration_path.read_bytes()).hexdigest()
            known[version] = (migration_path, checksum)

        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                checksum TEXT NOT NULL,
                applied_at REAL NOT NULL
            )
            """
        )
        columns = {
            str(row[1])
            for row in self._connection.execute("PRAGMA table_info(schema_migrations)")
        }
        if "checksum" not in columns:
            # Compatibility with the short-lived pre-checksum schema. Values are
            # populated and validated against the bundled immutable files below.
            self._connection.execute(
                "ALTER TABLE schema_migrations ADD COLUMN checksum TEXT"
            )

        applied_rows = self._connection.execute(
            "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
        newest_known = max(known)
        for row in applied_rows:
            version = int(row["version"])
            if version > newest_known:
                raise StorageError(
                    "SQLite schema is newer than this application; restore a compatible backup."
                )
            migration = known.get(version)
            if migration is None:
                raise StorageError(f"applied SQLite migration {version} is not bundled")
            migration_path, expected_checksum = migration
            if row["name"] != migration_path.name:
                raise StorageError(f"SQLite migration {version} has an unexpected name")
            stored_checksum = row["checksum"]
            if stored_checksum is None:
                self._connection.execute(
                    "UPDATE schema_migrations SET checksum = ? WHERE version = ?",
                    (expected_checksum, version),
                )
            elif stored_checksum != expected_checksum:
                raise StorageError(
                    f"SQLite migration {version} checksum does not match the bundled file"
                )

        applied = {int(row["version"]) for row in applied_rows}
        for version, (migration_path, checksum) in sorted(known.items()):
            if version in applied:
                continue
            script = migration_path.read_text(encoding="utf-8")
            safe_name = migration_path.name.replace("'", "''")
            applied_at = float(self._clock())
            try:
                self._connection.executescript(
                    "BEGIN IMMEDIATE;\n"
                    + script
                    + "\nINSERT INTO schema_migrations(version, name, checksum, applied_at) "
                    + f"VALUES ({version}, '{safe_name}', '{checksum}', {applied_at!r});\nCOMMIT;"
                )
            except sqlite3.Error:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self._closed:
                raise StorageError("SQLite storage is closed")
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                yield self._connection
            except Exception:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")

    def _now(self, now: float | None) -> float:
        return float(self._clock() if now is None else now)

    @staticmethod
    def _key_values(key: ConversationKey) -> tuple[str, str, str]:
        source = str(key.source).strip()
        if not source:
            raise ValueError("conversation source must not be empty")
        return source, str(key.channel_id), str(key.user_id)

    def _get_or_create_conversation_row(
        self, connection: sqlite3.Connection, key: ConversationKey, now: float
    ) -> sqlite3.Row:
        source, channel_id, user_id = self._key_values(key)
        connection.execute(
            """
            INSERT INTO conversations(source, channel_id, user_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(source, channel_id, user_id) DO NOTHING
            """,
            (source, channel_id, user_id, now, now),
        )
        row = connection.execute(
            """
            SELECT * FROM conversations
            WHERE source = ? AND channel_id = ? AND user_id = ?
            """,
            (source, channel_id, user_id),
        ).fetchone()
        assert row is not None
        return row

    def get_or_create_conversation(
        self, key: ConversationKey, *, now: float | None = None
    ) -> Conversation:
        timestamp = self._now(now)
        with self._transaction() as connection:
            row = self._get_or_create_conversation_row(connection, key, timestamp)
        return self._conversation_from_row(row)

    def get_conversation(self, key: ConversationKey) -> Conversation | None:
        source, channel_id, user_id = self._key_values(key)
        with self._lock:
            row = self._connection.execute(
                """
                SELECT * FROM conversations
                WHERE source = ? AND channel_id = ? AND user_id = ?
                """,
                (source, channel_id, user_id),
            ).fetchone()
        return None if row is None else self._conversation_from_row(row)

    def set_conversation_context(
        self,
        key: ConversationKey,
        *,
        active_repository: str | None | object = _UNSET,
        last_job_id: str | None | object = _UNSET,
        now: float | None = None,
    ) -> Conversation:
        timestamp = self._now(now)
        with self._transaction() as connection:
            row = self._get_or_create_conversation_row(connection, key, timestamp)
            repository = (
                row["active_repository"]
                if active_repository is _UNSET
                else active_repository
            )
            job_id = row["last_job_id"] if last_job_id is _UNSET else last_job_id
            connection.execute(
                """
                UPDATE conversations
                SET active_repository = ?, last_job_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (repository, job_id, timestamp, row["id"]),
            )
            updated = connection.execute(
                "SELECT * FROM conversations WHERE id = ?", (row["id"],)
            ).fetchone()
        assert updated is not None
        return self._conversation_from_row(updated)

    def get_conversation_for_request(self, request_id: str) -> Conversation | None:
        """Resolve conversation state from a transport-neutral request ID."""
        with self._lock:
            row = self._connection.execute(
                """
                SELECT c.*
                FROM conversations AS c
                JOIN inbound_requests AS r ON r.conversation_id = c.id
                WHERE r.request_id = ?
                """,
                (request_id,),
            ).fetchone()
        return None if row is None else self._conversation_from_row(row)

    def set_conversation_context_for_request(
        self,
        request_id: str,
        *,
        active_repository: str | None | object = _UNSET,
        last_job_id: str | None | object = _UNSET,
        now: float | None = None,
    ) -> Conversation:
        """Persist inferred context without leaking transport IDs into workers."""
        timestamp = self._now(now)
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT c.*, r.memory_generation AS request_memory_generation
                FROM conversations AS c
                JOIN inbound_requests AS r ON r.conversation_id = c.id
                WHERE r.request_id = ?
                """,
                (request_id,),
            ).fetchone()
            if row is None:
                raise KeyError(request_id)
            if int(row["request_memory_generation"]) != int(row["memory_generation"]):
                return self._conversation_from_row(row)
            repository = (
                row["active_repository"]
                if active_repository is _UNSET
                else active_repository
            )
            job_id = row["last_job_id"] if last_job_id is _UNSET else last_job_id
            connection.execute(
                """
                UPDATE conversations
                SET active_repository = ?, last_job_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (repository, job_id, timestamp, row["id"]),
            )
            updated = connection.execute(
                "SELECT * FROM conversations WHERE id = ?", (row["id"],)
            ).fetchone()
        assert updated is not None
        return self._conversation_from_row(updated)

    def register_inbound(
        self,
        message: InboundMessage,
        *,
        intent: Intent | str | None = None,
        backend: Backend | str | None = None,
        job_kind: JobKind | str | None = None,
        repository: str | None = None,
        payload: Mapping[str, Any] | None = None,
        now: float | None = None,
    ) -> InboundRegistration:
        """Atomically deduplicate a message and create its optional queued job."""
        if not isinstance(message.text, str) or not message.text.strip():
            raise ValueError("inbound message text must not be empty")

        timestamp = self._now(message.received_at if now is None else now)
        source = str(message.source).strip()
        external_id = str(message.message_id)
        request_id = deterministic_request_id(source, external_id)
        normalized_kind = None if job_kind is None else JobKind(_enum_value(job_kind))
        job_id = (
            None
            if normalized_kind is None
            else deterministic_job_id(request_id, normalized_kind)
        )
        payload_json = _json_object(payload)

        with self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT * FROM inbound_requests
                WHERE source = ? AND external_message_id = ?
                """,
                (source, external_id),
            ).fetchone()
            if existing is not None:
                conversation_row = connection.execute(
                    "SELECT * FROM conversations WHERE id = ?",
                    (existing["conversation_id"],),
                ).fetchone()
                job_row = (
                    None
                    if existing["job_id"] is None
                    else connection.execute(
                        "SELECT * FROM jobs WHERE job_id = ?", (existing["job_id"],)
                    ).fetchone()
                )
                assert conversation_row is not None
                return InboundRegistration(
                    request=self._request_from_row(existing),
                    conversation=self._conversation_from_row(conversation_row),
                    job=None if job_row is None else self._job_from_row(job_row),
                    created=False,
                )

            conversation_row = self._get_or_create_conversation_row(
                connection, message.conversation_key, timestamp
            )
            connection.execute(
                """
                INSERT INTO inbound_requests(
                    request_id, source, external_message_id, conversation_id,
                    content, intent, backend, status, job_id, memory_generation,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    source,
                    external_id,
                    conversation_row["id"],
                    message.text,
                    _enum_value(intent),
                    _enum_value(backend),
                    RequestStatus.RECEIVED.value,
                    job_id,
                    conversation_row["memory_generation"],
                    timestamp,
                    timestamp,
                ),
            )

            job_row = None
            if normalized_kind is not None and job_id is not None:
                connection.execute(
                    """
                    INSERT INTO jobs(
                        job_id, request_id, kind, repository, state, payload_json,
                        checkpoint_json, attempts, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, '{}', 0, ?, ?)
                    """,
                    (
                        job_id,
                        request_id,
                        normalized_kind.value,
                        repository,
                        JobStatus.QUEUED.value,
                        payload_json,
                        timestamp,
                        timestamp,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO job_events(job_id, from_state, to_state, message, created_at)
                    VALUES (?, NULL, ?, ?, ?)
                    """,
                    (job_id, JobStatus.QUEUED.value, "Trabajo registrado.", timestamp),
                )
                connection.execute(
                    """
                    UPDATE conversations
                    SET active_repository = COALESCE(?, active_repository),
                        last_job_id = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (repository, job_id, timestamp, conversation_row["id"]),
                )
                job_row = connection.execute(
                    "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
                ).fetchone()

            request_row = connection.execute(
                "SELECT * FROM inbound_requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            conversation_row = connection.execute(
                "SELECT * FROM conversations WHERE id = ?", (conversation_row["id"],)
            ).fetchone()
            assert request_row is not None and conversation_row is not None

        return InboundRegistration(
            request=self._request_from_row(request_row),
            conversation=self._conversation_from_row(conversation_row),
            job=None if job_row is None else self._job_from_row(job_row),
            created=True,
        )

    def ensure_job_for_request(
        self,
        request_id: str,
        kind: JobKind | str,
        *,
        repository: str | None = None,
        payload: Mapping[str, Any] | None = None,
        now: float | None = None,
    ) -> Job:
        """Create a queued job once when classification follows registration."""
        timestamp = self._now(now)
        normalized_kind = JobKind(_enum_value(kind))
        job_id = deterministic_job_id(request_id, normalized_kind)
        payload_json = _json_object(payload)
        with self._transaction() as connection:
            request = connection.execute(
                "SELECT * FROM inbound_requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            if request is None:
                raise KeyError(request_id)
            if request["job_id"] is not None:
                existing = connection.execute(
                    "SELECT * FROM jobs WHERE job_id = ?", (request["job_id"],)
                ).fetchone()
                if existing is None:
                    raise StorageError("inbound request refers to a missing job")
                if (
                    existing["kind"] != normalized_kind.value
                    or existing["repository"] != repository
                    or existing["payload_json"] != payload_json
                ):
                    raise StorageConflictError(
                        "request already owns a job with different content"
                    )
                return self._job_from_row(existing)

            connection.execute(
                """
                INSERT INTO jobs(
                    job_id, request_id, kind, repository, state, payload_json,
                    checkpoint_json, attempts, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, '{}', 0, ?, ?)
                """,
                (
                    job_id,
                    request_id,
                    normalized_kind.value,
                    repository,
                    JobStatus.QUEUED.value,
                    payload_json,
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                "UPDATE inbound_requests SET job_id = ?, updated_at = ? WHERE request_id = ?",
                (job_id, timestamp, request_id),
            )
            connection.execute(
                """
                INSERT INTO job_events(job_id, from_state, to_state, message, created_at)
                VALUES (?, NULL, ?, ?, ?)
                """,
                (job_id, JobStatus.QUEUED.value, "Trabajo registrado.", timestamp),
            )
            connection.execute(
                """
                UPDATE conversations
                SET active_repository = COALESCE(?, active_repository),
                    last_job_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (repository, job_id, timestamp, request["conversation_id"]),
            )
            job_row = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        assert job_row is not None
        return self._job_from_row(job_row)

    def get_inbound(self, request_id: str) -> InboundRequest | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM inbound_requests WHERE request_id = ?", (request_id,)
            ).fetchone()
        return None if row is None else self._request_from_row(row)

    def recent_aws_query_requests(
        self, key: ConversationKey, *, exclude_request_id: str, limit: int = 10
    ) -> list[InboundRequest]:
        """Read completed operational AWS requests without using model memory.

        Callers may inspect the bounded list to skip shorthand follow-ups. Failed
        requests, help responses, other users, requests before forget, and requests
        after the current request are excluded, so recovery selects the same history.
        """
        if not 1 <= limit <= 100:
            raise ValueError("AWS request history limit must be between 1 and 100")
        source, channel_id, user_id = self._key_values(key)
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT r.* FROM inbound_requests AS r
                JOIN conversations AS c ON c.id = r.conversation_id
                JOIN inbound_requests AS current ON current.request_id = ?
                  AND current.conversation_id = c.id
                  AND current.memory_generation = c.memory_generation
                WHERE c.source = ? AND c.channel_id = ? AND c.user_id = ?
                  AND r.memory_generation = c.memory_generation
                  AND (r.created_at < current.created_at
                       OR (r.created_at = current.created_at AND r.rowid < current.rowid))
                  AND r.intent = ? AND r.status = ?
                  AND EXISTS (
                      SELECT 1 FROM outbox AS o WHERE o.request_id = r.request_id
                      AND o.backend = ? AND o.kind = 'response'
                  )
                ORDER BY r.created_at DESC, r.rowid DESC LIMIT ?
                """,
                (exclude_request_id, source, channel_id, user_id, Intent.AWS_REPORT.value,
                 RequestStatus.COMPLETED.value, Backend.WORKER.value, limit),
            ).fetchall()
        return [self._request_from_row(row) for row in rows]

    def latest_aws_query_request(
        self, key: ConversationKey, *, exclude_request_id: str
    ) -> InboundRequest | None:
        requests = self.recent_aws_query_requests(key, exclude_request_id=exclude_request_id, limit=1)
        return requests[0] if requests else None

    def list_recoverable_direct_messages(
        self, *, limit: int | None = None
    ) -> list[InboundMessage]:
        """Reconstruct direct requests stranded before durable output creation."""
        if limit is not None and limit <= 0:
            raise ValueError("limit must be positive")
        sql = """
            SELECT r.external_message_id, r.source, r.content, r.created_at,
                   c.channel_id, c.user_id
            FROM inbound_requests AS r
            JOIN conversations AS c ON c.id = r.conversation_id
            WHERE r.job_id IS NULL
              AND r.status IN (?, ?, ?)
              AND NOT EXISTS (
                  SELECT 1 FROM outbox AS o WHERE o.request_id = r.request_id
              )
            ORDER BY r.created_at, r.rowid
        """
        parameters: list[Any] = [
            RequestStatus.RECEIVED.value,
            RequestStatus.PROCESSING.value,
            RequestStatus.FAILED.value,
        ]
        if limit is not None:
            sql += " LIMIT ?"
            parameters.append(limit)
        with self._lock:
            rows = self._connection.execute(sql, parameters).fetchall()
        return [
            InboundMessage(
                message_id=row["external_message_id"],
                channel_id=row["channel_id"],
                user_id=row["user_id"],
                text=row["content"],
                source=row["source"],
                received_at=float(row["created_at"]),
            )
            for row in rows
        ]

    def update_request_status(
        self,
        request_id: str,
        status: RequestStatus | str,
        *,
        now: float | None = None,
    ) -> InboundRequest:
        normalized = RequestStatus(_enum_value(status))
        timestamp = self._now(now)
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE inbound_requests SET status = ?, updated_at = ? WHERE request_id = ?",
                (normalized.value, timestamp, request_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(request_id)
            row = connection.execute(
                "SELECT * FROM inbound_requests WHERE request_id = ?", (request_id,)
            ).fetchone()
        assert row is not None
        return self._request_from_row(row)

    def get_job(self, job_id: str) -> Job | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return None if row is None else self._job_from_row(row)

    def list_jobs(
        self,
        *,
        states: Iterable[JobStatus | str] | None = None,
        limit: int | None = None,
    ) -> list[Job]:
        if limit is not None and limit <= 0:
            raise ValueError("limit must be positive")
        parameters: list[Any] = []
        sql = "SELECT * FROM jobs"
        if states is not None:
            normalized = [JobStatus(_enum_value(state)).value for state in states]
            if not normalized:
                return []
            placeholders = ",".join("?" for _ in normalized)
            sql += f" WHERE state IN ({placeholders})"
            parameters.extend(normalized)
        sql += " ORDER BY created_at, id"
        if limit is not None:
            sql += " LIMIT ?"
            parameters.append(limit)
        with self._lock:
            rows = self._connection.execute(sql, parameters).fetchall()
        return [self._job_from_row(row) for row in rows]

    def next_queued_job(self) -> Job | None:
        jobs = self.list_jobs(states=(JobStatus.QUEUED,), limit=1)
        return jobs[0] if jobs else None

    def list_jobs_pending_notification(self, *, limit: int | None = None) -> list[Job]:
        """Return completed outcomes whose final callback has not been ACKed."""
        if limit is not None and limit <= 0:
            raise ValueError("limit must be positive")
        states = tuple(state.value for state in _NOTIFIABLE_JOB_STATES)
        placeholders = ",".join("?" for _ in states)
        sql = (
            "SELECT * FROM jobs "
            f"WHERE notification_completed_at IS NULL AND state IN ({placeholders}) "
            "ORDER BY created_at, id"
        )
        parameters: list[Any] = list(states)
        if limit is not None:
            sql += " LIMIT ?"
            parameters.append(limit)
        with self._lock:
            rows = self._connection.execute(sql, parameters).fetchall()
        return [self._job_from_row(row) for row in rows]

    def mark_job_notification_completed(
        self, job_id: str, *, now: float | None = None
    ) -> Job:
        """Persist that the final callback durably prepared its notification."""
        timestamp = self._now(now)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            state = JobStatus(row["state"])
            if state not in _NOTIFIABLE_JOB_STATES:
                raise InvalidJobTransition(
                    f"job {job_id} in {state.value} has no final notification to ACK"
                )
            if row["notification_completed_at"] is None:
                connection.execute(
                    """
                    UPDATE jobs
                    SET notification_completed_at = ?, updated_at = ?
                    WHERE job_id = ?
                    """,
                    (timestamp, timestamp, job_id),
                )
                row = connection.execute(
                    "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
        assert row is not None
        return self._job_from_row(row)

    def transition_job(
        self,
        job_id: str,
        to_state: JobStatus | str,
        *,
        message: str | None = None,
        branch: str | None | object = _UNSET,
        external_reference: str | None | object = _UNSET,
        checkpoint: Mapping[str, Any] | object = _UNSET,
        summary: str | None | object = _UNSET,
        safe_error: str | None | object = _UNSET,
        validation_status: ValidationStatus | str | None | object = _UNSET,
        increment_attempt: bool | None = None,
        preserve_notification_ack: bool = False,
        now: float | None = None,
    ) -> Job:
        """Apply one validated transition and append its event atomically."""
        target = JobStatus(_enum_value(to_state))
        timestamp = self._now(now)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            current = JobStatus(row["state"])
            if target == current:
                return self._job_from_row(row)
            if target not in _ALLOWED_JOB_TRANSITIONS[current]:
                raise InvalidJobTransition(
                    f"job {job_id} cannot transition from {current.value} to {target.value}"
                )

            should_increment = (
                target is JobStatus.RUNNING
                if increment_attempt is None
                else increment_attempt
            )
            values = {
                "state": target.value,
                "branch": row["branch"] if branch is _UNSET else branch,
                "external_reference": (
                    row["external_reference"]
                    if external_reference is _UNSET
                    else external_reference
                ),
                "checkpoint_json": (
                    row["checkpoint_json"]
                    if checkpoint is _UNSET
                    else _json_object(checkpoint)
                ),
                "summary": row["summary"] if summary is _UNSET else summary,
                "safe_error": row["safe_error"] if safe_error is _UNSET else safe_error,
                "validation_status": (
                    row["validation_status"]
                    if validation_status is _UNSET
                    else _enum_value(validation_status)
                ),
                "attempts": int(row["attempts"]) + (1 if should_increment else 0),
                "updated_at": timestamp,
                "finished_at": timestamp if target.is_terminal else None,
                # Normally every distinct final outcome needs a new callback
                # acknowledgement. A PUBLISH wrapper is the sole owner of its
                # follow-up notification, so it can advance the original CODEX
                # row while atomically retaining the already-delivered ACK.
                "notification_completed_at": (
                    row["notification_completed_at"]
                    if preserve_notification_ack
                    else None
                ),
            }
            connection.execute(
                """
                UPDATE jobs
                SET state = :state,
                    branch = :branch,
                    external_reference = :external_reference,
                    checkpoint_json = :checkpoint_json,
                    summary = :summary,
                    safe_error = :safe_error,
                    validation_status = :validation_status,
                    attempts = :attempts,
                    updated_at = :updated_at,
                    finished_at = :finished_at,
                    notification_completed_at = :notification_completed_at
                WHERE job_id = :job_id
                """,
                {**values, "job_id": job_id},
            )
            connection.execute(
                """
                INSERT INTO job_events(job_id, from_state, to_state, message, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (job_id, current.value, target.value, message, timestamp),
            )
            updated = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        assert updated is not None
        return self._job_from_row(updated)

    def record_job_progress(
        self, job_id: str, message: str, *, now: float | None = None
    ) -> JobEvent:
        if not message.strip():
            raise ValueError("job progress message must not be empty")
        timestamp = self._now(now)
        with self._transaction() as connection:
            job = connection.execute(
                "SELECT state FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if job is None:
                raise KeyError(job_id)
            cursor = connection.execute(
                """
                INSERT INTO job_events(job_id, from_state, to_state, message, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (job_id, job["state"], job["state"], message, timestamp),
            )
            row = connection.execute(
                "SELECT * FROM job_events WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        assert row is not None
        return self._job_event_from_row(row)

    def update_running_job_checkpoint(
        self,
        job_id: str,
        checkpoint: Mapping[str, Any],
        *,
        now: float | None = None,
    ) -> Job:
        """Persist remote ownership before a long RUNNING operation returns."""
        timestamp = self._now(now)
        encoded = _json_object(checkpoint)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT state FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            state = JobStatus(row["state"])
            if state is not JobStatus.RUNNING:
                raise InvalidJobTransition(
                    f"job {job_id} cannot checkpoint while {state.value}"
                )
            connection.execute(
                """
                UPDATE jobs
                SET checkpoint_json = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (encoded, timestamp, job_id),
            )
            updated = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        assert updated is not None
        return self._job_from_row(updated)

    def remove_job_checkpoint_keys(
        self,
        job_id: str,
        keys: Iterable[str],
        *,
        now: float | None = None,
    ) -> Job:
        """Forget remote-resource ownership after cleanup, in any job state.

        Terminal jobs are immutable through the normal state machine, but a
        successfully deleted remote session must no longer be advertised as
        owned.  This narrow operation only removes explicitly named checkpoint
        keys and leaves state, result fields, events, and notification ACKs
        untouched.
        """
        normalized = tuple(dict.fromkeys(keys))
        if not normalized:
            raise ValueError("at least one checkpoint key is required")
        if any(not isinstance(key, str) or not _CHECKPOINT_KEY.fullmatch(key) for key in normalized):
            raise ValueError("checkpoint keys must be safe identifiers")

        timestamp = self._now(now)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            checkpoint = dict(_load_json_object(row["checkpoint_json"]))
            changed = False
            for key in normalized:
                if key in checkpoint:
                    del checkpoint[key]
                    changed = True
            if changed:
                connection.execute(
                    """
                    UPDATE jobs
                    SET checkpoint_json = ?, updated_at = ?
                    WHERE job_id = ?
                    """,
                    (_json_object(checkpoint), timestamp, job_id),
                )
                row = connection.execute(
                    "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
        assert row is not None
        return self._job_from_row(row)

    def list_job_events(self, job_id: str) -> list[JobEvent]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM job_events WHERE job_id = ? ORDER BY created_at, id",
                (job_id,),
            ).fetchall()
        return [self._job_event_from_row(row) for row in rows]

    def list_exchanges(
        self,
        key: ConversationKey,
        *,
        since: float | None = None,
        limit: int | None = None,
    ) -> list[Exchange]:
        if limit is not None and limit <= 0:
            raise ValueError("limit must be positive")
        conversation = self.get_conversation(key)
        if conversation is None:
            return []
        parameters: list[Any] = [conversation.id]
        where = "conversation_id = ?"
        if since is not None:
            where += " AND created_at >= ?"
            parameters.append(float(since))
        sql = f"SELECT * FROM exchanges WHERE {where} ORDER BY created_at DESC, id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            parameters.append(limit)
        with self._lock:
            rows = self._connection.execute(sql, parameters).fetchall()
        rows.reverse()
        return [self._exchange_from_row(row) for row in rows]

    def forget_conversation(
        self, key: ConversationKey, *, now: float | None = None
    ) -> Conversation:
        """Erase prompt memory while preserving operational audit/idempotency rows."""
        timestamp = self._now(now)
        with self._transaction() as connection:
            conversation = self._get_or_create_conversation_row(connection, key, timestamp)
            connection.execute(
                "DELETE FROM exchanges WHERE conversation_id = ?", (conversation["id"],)
            )
            connection.execute(
                """
                UPDATE conversations
                SET active_repository = NULL,
                    last_job_id = NULL,
                    memory_generation = memory_generation + 1,
                    updated_at = ?
                WHERE id = ?
                """,
                (timestamp, conversation["id"]),
            )
            updated = connection.execute(
                "SELECT * FROM conversations WHERE id = ?", (conversation["id"],)
            ).fetchone()
        assert updated is not None
        return self._conversation_from_row(updated)

    def delete_expired_exchanges(self, older_than: float) -> int:
        """Delete conversational memory older than an absolute timestamp."""
        with self._transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM exchanges WHERE created_at < ?", (float(older_than),)
            )
        return cursor.rowcount

    def create_outbox(
        self,
        request_id: str,
        *,
        kind: str,
        dedupe_key: str,
        assistant_text: str,
        parts: Sequence[str],
        backend: Backend | str | None,
        exchange_on_complete: bool,
        attachment: CsvAttachment | None = None,
        now: float | None = None,
    ) -> OutboxMessage:
        if not kind.strip() or not dedupe_key.strip():
            raise ValueError("outbox kind and dedupe_key must not be empty")
        if not isinstance(assistant_text, str) or not assistant_text.strip():
            raise ValueError("assistant_text must not be empty")
        if not parts or any(not isinstance(part, str) or not part for part in parts):
            raise ValueError("outbox parts must contain at least one non-empty string")
        if attachment is not None and not isinstance(attachment, CsvAttachment):
            raise ValueError("outbox attachment must be a CsvAttachment")
        if attachment is not None:
            # Exports and their potentially sensitive operational text never
            # become conversational context, even if a caller forgets the flag.
            exchange_on_complete = False
        timestamp = self._now(now)
        outbox_id = deterministic_outbox_id(request_id, kind, dedupe_key)
        backend_value = _enum_value(backend)
        with self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT * FROM outbox
                WHERE request_id = ? AND kind = ? AND dedupe_key = ?
                """,
                (request_id, kind, dedupe_key),
            ).fetchone()
            if existing is not None:
                existing_attachment = self._attachment_for_outbox(connection, existing["outbox_id"])
                existing_parts = [
                    row[0]
                    for row in connection.execute(
                        """
                        SELECT content FROM outbox_parts
                        WHERE outbox_id = ? ORDER BY part_index
                        """,
                        (existing["outbox_id"],),
                    )
                ]
                if (
                    existing["assistant_text"] != assistant_text
                    or existing["backend"] != backend_value
                    or bool(existing["exchange_on_complete"]) != exchange_on_complete
                    or existing_parts != list(parts)
                    or existing_attachment != attachment
                ):
                    raise StorageConflictError(
                        "outbox idempotency key was reused with different content"
                    )
                return self._outbox_from_row(existing, attachment=existing_attachment)

            request = connection.execute(
                "SELECT * FROM inbound_requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            if request is None:
                raise KeyError(request_id)
            try:
                connection.execute(
                    """
                    INSERT INTO outbox(
                        outbox_id, conversation_id, request_id, kind, dedupe_key,
                        assistant_text, backend, exchange_on_complete, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        outbox_id,
                        request["conversation_id"],
                        request_id,
                        kind,
                        dedupe_key,
                        assistant_text,
                        backend_value,
                        int(exchange_on_complete),
                        timestamp,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise StorageConflictError(
                    "request already has a different final memory output"
                ) from error
            connection.executemany(
                """
                INSERT INTO outbox_parts(outbox_id, part_index, content, created_at)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (outbox_id, index, content, timestamp)
                    for index, content in enumerate(parts)
                ],
            )
            if attachment is not None:
                connection.execute(
                    """
                    INSERT INTO outbox_attachments(outbox_id, filename, content_type, data, sha256)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (outbox_id, attachment.filename, attachment.content_type,
                     attachment.data, attachment.sha256),
                )
            row = connection.execute(
                "SELECT * FROM outbox WHERE outbox_id = ?", (outbox_id,)
            ).fetchone()
        assert row is not None
        return self._outbox_from_row(row, attachment=attachment)

    def get_outbox(self, outbox_id: str) -> OutboxMessage | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM outbox WHERE outbox_id = ?", (outbox_id,)
            ).fetchone()
            attachment = self._attachment_for_outbox(self._connection, outbox_id) if row is not None else None
        return None if row is None else self._outbox_from_row(row, attachment=attachment)

    def request_has_outbox(self, request_id: str) -> bool:
        """Return whether any durable output already owns this request."""
        with self._lock:
            row = self._connection.execute(
                "SELECT 1 FROM outbox WHERE request_id = ? LIMIT 1", (request_id,)
            ).fetchone()
        return row is not None

    def list_outbox_parts(self, outbox_id: str) -> list[OutboxPart]:
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT p.*, c.channel_id, c.user_id, {_ATTACHMENT_COLUMNS}
                FROM outbox_parts AS p
                JOIN outbox AS o ON o.outbox_id = p.outbox_id
                JOIN conversations AS c ON c.id = o.conversation_id
                LEFT JOIN outbox_attachments AS a ON a.outbox_id = o.outbox_id AND p.part_index = 0
                WHERE p.outbox_id = ?
                ORDER BY p.part_index
                """,
                (outbox_id,),
            ).fetchall()
        return [self._outbox_part_from_row(row) for row in rows]

    def list_pending_outbox_parts(
        self, key: ConversationKey | None = None
    ) -> list[OutboxPart]:
        parameters: list[Any] = []
        where = "o.completed_at IS NULL AND p.acked_at IS NULL"
        if key is not None:
            source, channel_id, user_id = self._key_values(key)
            where += " AND c.source = ? AND c.channel_id = ? AND c.user_id = ?"
            parameters.extend((source, channel_id, user_id))
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT p.*, c.channel_id, c.user_id, {_ATTACHMENT_COLUMNS}
                FROM outbox_parts AS p
                JOIN outbox AS o ON o.outbox_id = p.outbox_id
                JOIN conversations AS c ON c.id = o.conversation_id
                LEFT JOIN outbox_attachments AS a ON a.outbox_id = o.outbox_id AND p.part_index = 0
                WHERE {where}
                ORDER BY o.created_at, o.id, p.part_index
                """,
                parameters,
            ).fetchall()
        return [self._outbox_part_from_row(row) for row in rows]

    def acknowledge_outbox_part(
        self,
        outbox_id: str,
        part_index: int,
        discord_message_id: str | int,
        *,
        now: float | None = None,
    ) -> OutboxPart:
        """ACK one part and materialize memory only after the final part ACK."""
        if part_index < 0:
            raise ValueError("part_index must not be negative")
        remote_id = str(discord_message_id).strip()
        if not remote_id:
            raise ValueError("discord_message_id must not be empty")
        timestamp = self._now(now)
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT p.*, c.channel_id, c.user_id,
                       o.request_id, o.assistant_text, o.backend,
                       o.exchange_on_complete, o.completed_at
                FROM outbox_parts AS p
                JOIN outbox AS o ON o.outbox_id = p.outbox_id
                JOIN conversations AS c ON c.id = o.conversation_id
                WHERE p.outbox_id = ? AND p.part_index = ?
                """,
                (outbox_id, part_index),
            ).fetchone()
            if row is None:
                raise KeyError((outbox_id, part_index))

            if row["acked_at"] is None:
                connection.execute(
                    """
                    UPDATE outbox_parts
                    SET discord_message_id = ?, acked_at = ?
                    WHERE outbox_id = ? AND part_index = ?
                    """,
                    (remote_id, timestamp, outbox_id, part_index),
                )

            pending = connection.execute(
                """
                SELECT 1 FROM outbox_parts
                WHERE outbox_id = ? AND acked_at IS NULL LIMIT 1
                """,
                (outbox_id,),
            ).fetchone()
            if pending is None and row["completed_at"] is None:
                connection.execute(
                    "UPDATE outbox SET completed_at = ? WHERE outbox_id = ?",
                    (timestamp, outbox_id),
                )
                if bool(row["exchange_on_complete"]):
                    request = connection.execute(
                        "SELECT * FROM inbound_requests WHERE request_id = ?",
                        (row["request_id"],),
                    ).fetchone()
                    if request is None:
                        raise StorageError("outbox request disappeared before final ACK")
                    conversation = connection.execute(
                        "SELECT memory_generation FROM conversations WHERE id = ?",
                        (request["conversation_id"],),
                    ).fetchone()
                    if (
                        conversation is not None
                        and int(request["memory_generation"])
                        == int(conversation["memory_generation"])
                    ):
                        connection.execute(
                            """
                            INSERT INTO exchanges(
                                conversation_id, request_id, user_text, assistant_text,
                                backend, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?)
                            ON CONFLICT(request_id) DO NOTHING
                            """,
                            (
                                request["conversation_id"],
                                request["request_id"],
                                request["content"],
                                row["assistant_text"],
                                row["backend"],
                                timestamp,
                            ),
                        )
                    connection.execute(
                        """
                        UPDATE inbound_requests
                        SET status = ?, updated_at = ? WHERE request_id = ?
                        """,
                        (RequestStatus.COMPLETED.value, timestamp, row["request_id"]),
                    )

            updated = connection.execute(
                f"""
                SELECT p.*, c.channel_id, c.user_id, {_ATTACHMENT_COLUMNS}
                FROM outbox_parts AS p
                JOIN outbox AS o ON o.outbox_id = p.outbox_id
                JOIN conversations AS c ON c.id = o.conversation_id
                LEFT JOIN outbox_attachments AS a ON a.outbox_id = o.outbox_id AND p.part_index = 0
                WHERE p.outbox_id = ? AND p.part_index = ?
                """,
                (outbox_id, part_index),
            ).fetchone()
        assert updated is not None
        return self._outbox_part_from_row(updated)

    def prune(
        self,
        *,
        memory_retention_seconds: float,
        operational_retention_seconds: float,
        now: float | None = None,
    ) -> tuple[int, int]:
        """Prune expired memory and terminal operational rows.

        Returns ``(deleted_exchanges, deleted_requests)``. Non-terminal work is
        never removed regardless of age.
        """
        if memory_retention_seconds <= 0 or operational_retention_seconds <= 0:
            raise ValueError("retention periods must be positive")
        timestamp = self._now(now)
        memory_cutoff = timestamp - float(memory_retention_seconds)
        operation_cutoff = timestamp - float(operational_retention_seconds)
        terminal_states = (
            JobStatus.SUCCEEDED.value,
            JobStatus.FAILED.value,
            JobStatus.CANCELLED.value,
        )
        with self._transaction() as connection:
            exchange_cursor = connection.execute(
                "DELETE FROM exchanges WHERE created_at < ?", (memory_cutoff,)
            )
            connection.execute(
                """
                UPDATE conversations
                SET last_job_id = NULL
                WHERE last_job_id IN (
                    SELECT jobs.job_id
                    FROM jobs
                    JOIN inbound_requests AS requests
                      ON requests.job_id = jobs.job_id
                    WHERE jobs.state IN (?, ?, ?)
                      AND jobs.finished_at IS NOT NULL AND jobs.finished_at < ?
                      AND jobs.notification_completed_at IS NOT NULL
                      AND COALESCE(
                          json_extract(jobs.checkpoint_json, '$.opencode_session_id'),
                          ''
                      ) = ''
                      AND COALESCE(
                          json_extract(jobs.checkpoint_json, '$.preflight_session_id'),
                          ''
                      ) = ''
                      AND NOT EXISTS (
                          SELECT 1 FROM outbox
                          WHERE outbox.request_id = requests.request_id
                            AND outbox.completed_at IS NULL
                      )
                )
                """,
                (*terminal_states, operation_cutoff),
            )
            request_cursor = connection.execute(
                """
                DELETE FROM inbound_requests
                WHERE (
                    (
                        job_id IS NULL
                        AND status IN ('completed', 'failed')
                        AND updated_at < ?
                    ) OR job_id IN (
                        SELECT job_id FROM jobs
                        WHERE state IN (?, ?, ?)
                          AND finished_at IS NOT NULL AND finished_at < ?
                          AND notification_completed_at IS NOT NULL
                          AND COALESCE(
                              json_extract(
                                  jobs.checkpoint_json, '$.opencode_session_id'
                              ),
                              ''
                          ) = ''
                          AND COALESCE(
                              json_extract(
                                  jobs.checkpoint_json, '$.preflight_session_id'
                              ),
                              ''
                          ) = ''
                    )
                ) AND NOT EXISTS (
                    SELECT 1 FROM outbox
                    WHERE outbox.request_id = inbound_requests.request_id
                      AND outbox.completed_at IS NULL
                )
                """,
                (operation_cutoff, *terminal_states, operation_cutoff),
            )
        return exchange_cursor.rowcount, request_cursor.rowcount

    @staticmethod
    def _conversation_from_row(row: sqlite3.Row) -> Conversation:
        return Conversation(
            id=int(row["id"]),
            key=ConversationKey(row["source"], row["channel_id"], row["user_id"]),
            active_repository=row["active_repository"],
            last_job_id=row["last_job_id"],
            memory_generation=int(row["memory_generation"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    @staticmethod
    def _request_from_row(row: sqlite3.Row) -> InboundRequest:
        return InboundRequest(
            request_id=row["request_id"],
            source=row["source"],
            external_message_id=row["external_message_id"],
            conversation_id=int(row["conversation_id"]),
            text=row["content"],
            intent=None if row["intent"] is None else Intent(row["intent"]),
            backend=None if row["backend"] is None else Backend(row["backend"]),
            status=RequestStatus(row["status"]),
            job_id=row["job_id"],
            memory_generation=int(row["memory_generation"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    @staticmethod
    def _exchange_from_row(row: sqlite3.Row) -> Exchange:
        return Exchange(
            id=int(row["id"]),
            conversation_id=int(row["conversation_id"]),
            request_id=row["request_id"],
            user_text=row["user_text"],
            assistant_text=row["assistant_text"],
            backend=None if row["backend"] is None else Backend(row["backend"]),
            created_at=float(row["created_at"]),
        )

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> Job:
        return Job(
            job_id=row["job_id"],
            request_id=row["request_id"],
            kind=JobKind(row["kind"]),
            state=JobStatus(row["state"]),
            repository=row["repository"],
            branch=row["branch"],
            external_reference=row["external_reference"],
            payload=_load_json_object(row["payload_json"]),
            checkpoint=_load_json_object(row["checkpoint_json"]),
            summary=row["summary"],
            safe_error=row["safe_error"],
            validation_status=(
                None
                if row["validation_status"] is None
                else ValidationStatus(row["validation_status"])
            ),
            attempts=int(row["attempts"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            finished_at=(
                None if row["finished_at"] is None else float(row["finished_at"])
            ),
            notification_completed_at=(
                None
                if row["notification_completed_at"] is None
                else float(row["notification_completed_at"])
            ),
        )

    @staticmethod
    def _job_event_from_row(row: sqlite3.Row) -> JobEvent:
        return JobEvent(
            id=int(row["id"]),
            job_id=row["job_id"],
            from_state=(
                None if row["from_state"] is None else JobStatus(row["from_state"])
            ),
            to_state=JobStatus(row["to_state"]),
            message=row["message"],
            created_at=float(row["created_at"]),
        )

    @staticmethod
    def _outbox_from_row(row: sqlite3.Row, *, attachment: CsvAttachment | None = None) -> OutboxMessage:
        return OutboxMessage(
            outbox_id=row["outbox_id"],
            conversation_id=int(row["conversation_id"]),
            request_id=row["request_id"],
            kind=row["kind"],
            assistant_text=row["assistant_text"],
            backend=None if row["backend"] is None else Backend(row["backend"]),
            exchange_on_complete=bool(row["exchange_on_complete"]),
            created_at=float(row["created_at"]),
            completed_at=(
                None if row["completed_at"] is None else float(row["completed_at"])
            ),
            attachment=attachment,
        )

    @staticmethod
    def _outbox_part_from_row(row: sqlite3.Row) -> OutboxPart:
        return OutboxPart(
            outbox_id=row["outbox_id"],
            part_index=int(row["part_index"]),
            content=row["content"],
            channel_id=row["channel_id"],
            user_id=row["user_id"],
            discord_message_id=row["discord_message_id"],
            acked_at=None if row["acked_at"] is None else float(row["acked_at"]),
            created_at=float(row["created_at"]),
            attachment=SQLiteStorage._attachment_from_row(row),
        )

    @staticmethod
    def _attachment_for_outbox(connection: sqlite3.Connection, outbox_id: str) -> CsvAttachment | None:
        row = connection.execute(
            f"SELECT {_ATTACHMENT_COLUMNS} FROM outbox_attachments AS a WHERE a.outbox_id = ?",
            (outbox_id,),
        ).fetchone()
        return None if row is None else SQLiteStorage._attachment_from_row(row)

    @staticmethod
    def _attachment_from_row(row: sqlite3.Row) -> CsvAttachment | None:
        if row["attachment_filename"] is None:
            return None
        try:
            attachment = CsvAttachment(
                filename=row["attachment_filename"], data=row["attachment_data"],
                content_type=row["attachment_content_type"],
            )
        except (TypeError, ValueError) as error:
            raise StorageError("Stored CSV attachment is invalid") from error
        if attachment.sha256 != row["attachment_sha256"]:
            raise StorageError("Stored CSV attachment failed its integrity check")
        return attachment
