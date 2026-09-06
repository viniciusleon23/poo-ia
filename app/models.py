"""Neutral domain models shared by Poo-IA's adapters and core.

The models in this module deliberately contain no Discord, SQLite, HTTP, or
worker objects.  Keeping that boundary makes the same core usable by the future
web interface.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class Intent(str, Enum):
    """User intentions understood by the deterministic router."""

    CHAT = "chat"
    CAPABILITIES = "capabilities"
    RESEARCH = "research"
    AWS_REPORT = "aws_report"
    CODE_CHANGE = "code_change"
    PULL_REQUEST = "pull_request"
    JOB_STATUS = "job_status"
    CANCEL = "cancel"
    FORGET = "forget"
    CLARIFY = "clarify"


class Backend(str, Enum):
    """Execution boundary selected for a routed request."""

    OLLAMA = "ollama"
    OPENCODE = "opencode"
    WORKER = "worker"
    STORAGE = "storage"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """A routing result that is independent of any transport."""

    intent: Intent
    backend: Backend
    repository: str | None = None
    job_id: str | None = None
    reason: str | None = None


class RequestStatus(str, Enum):
    """Lifecycle of a deduplicated inbound transport request."""

    RECEIVED = "received"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class JobKind(str, Enum):
    """Kinds of persistent work coordinated by the core."""

    RESEARCH = "research"
    CODEX = "codex"
    PUBLISH = "publish"
    AWS = "aws"


class JobStatus(str, Enum):
    """Canonical persistent job states."""

    QUEUED = "queued"
    RUNNING = "running"
    PREPARED = "prepared"
    PUBLISHING = "publishing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in {self.SUCCEEDED, self.FAILED, self.CANCELLED}


class ValidationStatus(str, Enum):
    """Outcome of repository validation performed by the host worker."""

    PASSED = "passed"
    UNCHANGED_FAILURE = "unchanged_failure"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"
    TIMED_OUT = "timed_out"


@dataclass(frozen=True, slots=True)
class ConversationKey:
    """Stable identity of one user's conversation in a transport channel."""

    source: str
    channel_id: str | int
    user_id: str | int


@dataclass(frozen=True, slots=True)
class InboundMessage:
    """Transport-neutral user message accepted by an adapter."""

    message_id: str | int
    channel_id: str | int
    user_id: str | int
    text: str
    source: str = "discord"
    received_at: float | None = None

    @property
    def conversation_key(self) -> ConversationKey:
        return ConversationKey(self.source, self.channel_id, self.user_id)


@dataclass(frozen=True, slots=True)
class Conversation:
    id: int
    key: ConversationKey
    active_repository: str | None
    last_job_id: str | None
    memory_generation: int
    created_at: float
    updated_at: float


@dataclass(frozen=True, slots=True)
class InboundRequest:
    request_id: str
    source: str
    external_message_id: str
    conversation_id: int
    text: str
    intent: Intent | None
    backend: Backend | None
    status: RequestStatus
    job_id: str | None
    memory_generation: int
    created_at: float
    updated_at: float


@dataclass(frozen=True, slots=True)
class Exchange:
    id: int
    conversation_id: int
    request_id: str
    user_text: str
    assistant_text: str
    backend: Backend | None
    created_at: float


@dataclass(frozen=True, slots=True)
class Job:
    job_id: str
    request_id: str
    kind: JobKind
    state: JobStatus
    repository: str | None
    branch: str | None
    external_reference: str | None
    payload: Mapping[str, Any]
    checkpoint: Mapping[str, Any]
    summary: str | None
    safe_error: str | None
    validation_status: ValidationStatus | None
    attempts: int
    created_at: float
    updated_at: float
    finished_at: float | None
    notification_completed_at: float | None


@dataclass(frozen=True, slots=True)
class JobEvent:
    id: int
    job_id: str
    from_state: JobStatus | None
    to_state: JobStatus
    message: str | None
    created_at: float


@dataclass(frozen=True, slots=True)
class OutboxMessage:
    outbox_id: str
    conversation_id: int
    request_id: str
    kind: str
    assistant_text: str
    backend: Backend | None
    exchange_on_complete: bool
    created_at: float
    completed_at: float | None

    @property
    def is_complete(self) -> bool:
        return self.completed_at is not None


@dataclass(frozen=True, slots=True)
class OutboxPart:
    outbox_id: str
    part_index: int
    content: str
    channel_id: str
    user_id: str
    discord_message_id: str | None
    acked_at: float | None
    created_at: float

    @property
    def is_acknowledged(self) -> bool:
        return self.acked_at is not None


@dataclass(frozen=True, slots=True)
class InboundRegistration:
    """Result of atomically registering one external message and optional job."""

    request: InboundRequest
    conversation: Conversation
    job: Job | None
    created: bool


@dataclass(frozen=True, slots=True)
class MemorySnapshot:
    """Completed conversational context safe to place in a model prompt."""

    conversation: Conversation
    exchanges: tuple[Exchange, ...] = field(default_factory=tuple)
    rendered: str = ""
