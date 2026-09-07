"""Persistent, pre-split Discord delivery queue."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from .models import Backend, ConversationKey, CsvAttachment, OutboxMessage, OutboxPart
from .storage import SQLiteStorage
from .text import DISCORD_SAFE_MESSAGE_LIMIT, split_for_discord


DEFAULT_MAX_OUTPUT_CHARS = DISCORD_SAFE_MESSAGE_LIMIT * 20
_TRUNCATION_MARKER = "\n\n[Respuesta recortada por el límite de almacenamiento.]"


class DurableOutbox:
    """Prepare outputs once and ACK each Discord fragment durably."""

    def __init__(
        self,
        storage: SQLiteStorage,
        *,
        message_limit: int = DISCORD_SAFE_MESSAGE_LIMIT,
        max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
        clock: callable = time.time,
    ) -> None:
        if message_limit <= 0 or message_limit > DISCORD_SAFE_MESSAGE_LIMIT:
            raise ValueError(
                f"message_limit must be between 1 and {DISCORD_SAFE_MESSAGE_LIMIT}"
            )
        if max_output_chars < message_limit:
            raise ValueError("max_output_chars must be at least message_limit")
        self.storage = storage
        self.message_limit = message_limit
        self.max_output_chars = max_output_chars
        self._clock = clock

    def enqueue(
        self,
        request_id: str,
        text: str,
        *,
        backend: Backend | str | None = None,
        kind: str = "response",
        dedupe_key: str = "final",
        remember_exchange: bool = True,
        attachment: CsvAttachment | None = None,
        now: float | None = None,
    ) -> OutboxMessage:
        """Idempotently enqueue a complete, already generated output."""
        if len(text) > self.max_output_chars:
            available = self.max_output_chars - len(_TRUNCATION_MARKER)
            text = text[:available].rstrip() + _TRUNCATION_MARKER
        parts = split_for_discord(text, limit=self.message_limit)
        timestamp = float(self._clock() if now is None else now)
        return self.storage.create_outbox(
            request_id,
            kind=kind,
            dedupe_key=dedupe_key,
            assistant_text=text,
            parts=parts,
            backend=backend,
            exchange_on_complete=remember_exchange,
            attachment=attachment,
            now=timestamp,
        )

    def enqueue_progress(
        self,
        request_id: str,
        text: str,
        *,
        dedupe_key: str,
        now: float | None = None,
    ) -> OutboxMessage:
        """Enqueue operational progress that must never enter model memory."""
        return self.enqueue(
            request_id,
            text,
            kind="progress",
            dedupe_key=dedupe_key,
            remember_exchange=False,
            now=now,
        )

    def pending(self, key: ConversationKey | None = None) -> list[OutboxPart]:
        """Return unacknowledged parts in durable creation/part order."""
        return self.storage.list_pending_outbox_parts(key)

    def parts(self, outbox_id: str) -> list[OutboxPart]:
        return self.storage.list_outbox_parts(outbox_id)

    def acknowledge(
        self,
        part: OutboxPart,
        discord_message_id: str | int,
        *,
        now: float | None = None,
    ) -> OutboxPart:
        timestamp = float(self._clock() if now is None else now)
        return self.storage.acknowledge_outbox_part(
            part.outbox_id,
            part.part_index,
            discord_message_id,
            now=timestamp,
        )

    async def flush(
        self,
        sender: Callable[[OutboxPart], Awaitable[str | int | Any]],
        *,
        key: ConversationKey | None = None,
    ) -> int:
        """Send pending parts sequentially, stopping safely on sender failure.

        ``sender`` receives the full part (including its channel) and may return
        a Discord ID directly or an object with an ``id`` attribute.
        """
        sent = 0
        for part in self.pending(key):
            result = await sender(part)
            remote_id = getattr(result, "id", result)
            if remote_id is None:
                raise ValueError("outbox sender did not return a message ID")
            self.acknowledge(part, remote_id)
            sent += 1
        return sent


# Short name for dependency injection in the orchestrator/Discord adapter.
Outbox = DurableOutbox
