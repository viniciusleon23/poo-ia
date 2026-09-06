"""Conversation memory policy and per-conversation async serialization."""

from __future__ import annotations

import asyncio
import re
import time
import unicodedata
from contextlib import asynccontextmanager
from typing import AsyncIterator, Sequence

from .models import ConversationKey, Exchange, MemorySnapshot
from .storage import SQLiteStorage


DEFAULT_RETENTION_DAYS = 7
DEFAULT_MAX_EXCHANGES = 10
DEFAULT_MAX_CONTEXT_CHARS = 12_000
FORGET_PHRASE = "olvida la conversacion"

_HISTORY_HEADER = "## Historial reciente de la conversación\n"
_TURN_TEMPLATE = (
    "\n<intercambio>\n"
    "Usuario:\n{user}\n"
    "Asistente:\n{assistant}\n"
    "</intercambio>\n"
)


def _normalize_control(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    without_accents = "".join(
        character
        for character in decomposed
        if unicodedata.category(character) != "Mn"
    )
    return re.sub(r"[^a-z0-9]+", " ", without_accents).strip()


def is_forget_request(text: str) -> bool:
    """Recognize only the exact normalized memory deletion control."""
    return isinstance(text, str) and _normalize_control(text) == FORGET_PHRASE


def _render_turn(exchange: Exchange) -> str:
    return _TURN_TEMPLATE.format(
        user=exchange.user_text.strip(), assistant=exchange.assistant_text.strip()
    )


def _render_truncated_turn(exchange: Exchange, budget: int) -> str:
    """Render the newest oversized turn without exceeding ``budget``."""
    marker = "\n[Intercambio reciente recortado por longitud]\n"
    scaffolding = _TURN_TEMPLATE.format(user="", assistant="")
    available = budget - len(_HISTORY_HEADER) - len(marker) - len(scaffolding)
    if available <= 0:
        return ""

    # The user's request is slightly more valuable for resolving a follow-up,
    # while retaining a useful portion of the assistant result too.
    user_budget = min(len(exchange.user_text), (available * 3) // 5)
    assistant_budget = min(len(exchange.assistant_text), available - user_budget)
    unused = available - user_budget - assistant_budget
    if unused:
        extra_user = min(unused, len(exchange.user_text) - user_budget)
        user_budget += extra_user
        assistant_budget += min(
            unused - extra_user, len(exchange.assistant_text) - assistant_budget
        )

    user = exchange.user_text[-user_budget:] if user_budget else ""
    assistant = (
        exchange.assistant_text[-assistant_budget:] if assistant_budget else ""
    )
    rendered = _HISTORY_HEADER + marker + _TURN_TEMPLATE.format(
        user=user, assistant=assistant
    )
    return rendered[:budget]


def format_history(
    exchanges: Sequence[Exchange], *, max_chars: int = DEFAULT_MAX_CONTEXT_CHARS
) -> str:
    """Format completed turns, evicting oldest turns to fit the prompt budget."""
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if not exchanges:
        return ""

    turns = [_render_turn(exchange) for exchange in exchanges]
    while len(turns) > 1 and len(_HISTORY_HEADER) + sum(map(len, turns)) > max_chars:
        turns.pop(0)

    rendered = _HISTORY_HEADER + "".join(turns)
    if len(rendered) <= max_chars:
        return rendered
    return _render_truncated_turn(exchanges[-1], max_chars)


class ConversationLockPool:
    """Async locks keyed by conversation, preventing interleaved follow-ups."""

    def __init__(self) -> None:
        self._locks: dict[tuple[str, str, str], asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    @staticmethod
    def _identity(key: ConversationKey) -> tuple[str, str, str]:
        return str(key.source), str(key.channel_id), str(key.user_id)

    @asynccontextmanager
    async def hold(self, key: ConversationKey) -> AsyncIterator[None]:
        identity = self._identity(key)
        async with self._guard:
            lock = self._locks.setdefault(identity, asyncio.Lock())
        async with lock:
            yield


class MemoryStore:
    """Apply retention, count, character, isolation, and forget policies."""

    def __init__(
        self,
        storage: SQLiteStorage,
        *,
        retention_days: float = DEFAULT_RETENTION_DAYS,
        max_exchanges: int = DEFAULT_MAX_EXCHANGES,
        max_context_chars: int = DEFAULT_MAX_CONTEXT_CHARS,
        clock: callable = time.time,
    ) -> None:
        if retention_days <= 0:
            raise ValueError("retention_days must be positive")
        if max_exchanges <= 0:
            raise ValueError("max_exchanges must be positive")
        if max_context_chars <= 0:
            raise ValueError("max_context_chars must be positive")
        self.storage = storage
        self.retention_seconds = float(retention_days) * 86_400
        self.max_exchanges = max_exchanges
        self.max_context_chars = max_context_chars
        self._clock = clock
        self._locks = ConversationLockPool()

    def snapshot(
        self, key: ConversationKey, *, now: float | None = None
    ) -> MemorySnapshot:
        timestamp = float(self._clock() if now is None else now)
        cutoff = timestamp - self.retention_seconds
        self.storage.delete_expired_exchanges(cutoff)
        conversation = self.storage.get_or_create_conversation(key, now=timestamp)
        exchanges = tuple(
            self.storage.list_exchanges(
                key, since=cutoff, limit=self.max_exchanges
            )
        )
        return MemorySnapshot(
            conversation=conversation,
            exchanges=exchanges,
            rendered=format_history(exchanges, max_chars=self.max_context_chars),
        )

    # Readable alias for callers that only need prompt text.
    def load_context(self, key: ConversationKey, *, now: float | None = None) -> str:
        return self.snapshot(key, now=now).rendered

    def forget(
        self, key: ConversationKey, *, now: float | None = None
    ) -> MemorySnapshot:
        timestamp = float(self._clock() if now is None else now)
        conversation = self.storage.forget_conversation(key, now=timestamp)
        return MemorySnapshot(conversation=conversation)

    def recognizes_forget(self, text: str) -> bool:
        return is_forget_request(text)

    @asynccontextmanager
    async def lock(self, key: ConversationKey) -> AsyncIterator[None]:
        async with self._locks.hold(key):
            yield
