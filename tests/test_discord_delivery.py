from __future__ import annotations

import unittest
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.config import Settings
from app.discord_bot import PROCESSING_FAILED_MESSAGE, PooIAClient
from app.models import ConversationKey, CsvAttachment, InboundMessage, OutboxPart


def settings() -> Settings:
    return Settings(
        discord_token="not-a-token",
        discord_channel_id=100,
        ollama_model="qwen2.5-coder:3b",
        ollama_base_url="http://127.0.0.1:11434",
        allowed_user_id=200,
        ollama_timeout_seconds=120,
        content_root=Path("."),
    )


class FakeChannel:
    def __init__(self, channel_id: int = 100) -> None:
        self.id = channel_id
        self.sent: list[str] = []

    @asynccontextmanager
    async def typing(self):
        yield

    async def send(self, content: str):
        self.sent.append(content)
        return SimpleNamespace(id=900 + len(self.sent))


class FakeOrchestrator:
    def __init__(self, part: OutboxPart) -> None:
        self.part = part
        self.handled: list[InboundMessage] = []
        self.keys: list[ConversationKey | None] = []
        self.failure_calls: list[tuple[InboundMessage, str]] = []
        self.handle_error: Exception | None = None
        self.failure_error: Exception | None = None

    async def handle(self, message: InboundMessage) -> None:
        self.handled.append(message)
        if self.handle_error is not None:
            raise self.handle_error

    async def record_processing_failure(
        self, message: InboundMessage, *, safe_message: str
    ) -> bool:
        self.failure_calls.append((message, safe_message))
        if self.failure_error is not None:
            raise self.failure_error
        self.part = part(safe_message)
        return True

    async def flush_outputs(self, sender, *, key=None) -> int:
        self.keys.append(key)
        await sender(self.part)
        return 1

    async def wait_for_output(self, timeout=None) -> bool:
        return True

    def set_repositories(self, repositories) -> tuple[str, ...]:
        self.repositories = tuple(repositories)
        return self.repositories


class FakeInventoryWorker:
    async def list_repositories(self) -> tuple[str, ...]:
        return ("brain-capnet", "capnet-next-lambda-tasks")


class FakePrunableStorage:
    def __init__(self) -> None:
        self.calls: list[dict[str, float]] = []

    def prune(self, **kwargs: float) -> tuple[int, int]:
        self.calls.append(kwargs)
        return (0, 0)


class AdapterClient(PooIAClient):
    def __init__(self, channel: FakeChannel) -> None:
        super().__init__(settings())
        self.fake_channel = channel
        self.fetches: list[int] = []
        self.use_cache = True

    def get_channel(self, channel_id: int):
        return self.fake_channel if self.use_cache and channel_id == self.fake_channel.id else None

    async def fetch_channel(self, channel_id: int):
        self.fetches.append(channel_id)
        return self.fake_channel


class DisconnectedAdapterClient(AdapterClient):
    def __init__(self, channel: FakeChannel) -> None:
        super().__init__(channel)
        self.closed_checks = 0

    def is_closed(self) -> bool:
        self.closed_checks += 1
        return self.closed_checks > 1

    def is_ready(self) -> bool:
        return False


def part(content: str = "respuesta") -> OutboxPart:
    return OutboxPart(
        outbox_id="outbox-1",
        part_index=0,
        content=content,
        channel_id="100",
        user_id="200",
        discord_message_id=None,
        acked_at=None,
        created_at=1.0,
    )


class DiscordDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_receipt_precedes_processing_and_visual_failures_do_not_abort_it(self) -> None:
        events = []
        class BrokenTyping(FakeChannel):
            @asynccontextmanager
            async def typing(self):
                events.append("typing")
                raise ConnectionError("typing unavailable")
                yield
        channel = BrokenTyping()
        client = AdapterClient(channel)
        self.addAsyncCleanup(client.close)
        orchestrator = FakeOrchestrator(part())
        original = orchestrator.handle
        async def handle(message):
            events.append("handle")
            await original(message)
        orchestrator.handle = handle
        client._orchestrator = orchestrator
        async def react(emoji):
            events.append(emoji)
            raise ConnectionError("reaction unavailable")
        message = SimpleNamespace(id=801, channel=channel, content="hola", author=SimpleNamespace(id=200, bot=False), add_reaction=react)
        await client.on_message(message)
        self.assertEqual(events, ["👀", "typing", "handle"])
        self.assertEqual(channel.sent, ["respuesta"])
        self.assertEqual(orchestrator.failure_calls, [])
        client._orchestrator = None

    async def test_slow_inventory_cannot_hold_feedback_indefinitely(self) -> None:
        client = AdapterClient(FakeChannel())
        self.addAsyncCleanup(client.close)
        started = asyncio.Event()
        async def slow_refresh(*, force=False):
            started.set()
            await asyncio.Event().wait()
        client._refresh_worker_repositories = slow_refresh
        with patch("app.discord_bot.REPOSITORY_REFRESH_TIMEOUT_SECONDS", 0.01):
            await asyncio.wait_for(client._bounded_repository_refresh(), timeout=0.2)
        self.assertTrue(started.is_set())

    async def test_csv_and_text_share_one_send_and_retry_recreates_the_file(self) -> None:
        class AttachmentChannel(FakeChannel):
            def __init__(self):
                super().__init__()
                self.uploads = []
                self.streams = []

            async def send(self, content, *, file, allowed_mentions):
                self.uploads.append((content, file.filename, file.fp.read(), allowed_mentions.to_dict()))
                self.streams.append(file.fp)
                if len(self.uploads) == 1:
                    raise ConnectionError("interrupted upload")
                return SimpleNamespace(id=902)

        channel = AttachmentChannel()
        client = AdapterClient(channel)
        csv = CsvAttachment("tasks.csv", b"id,available\n1,true\n")
        output = replace(part("Resultado @everyone"), attachment=csv)
        with self.assertRaises(ConnectionError):
            await client._send_outbox_part(output)
        sent = await client._send_outbox_part(output)

        self.assertEqual(sent.id, 902)
        self.assertEqual(channel.uploads[0], channel.uploads[1])
        self.assertEqual(channel.uploads[0][:3], ("Resultado @everyone", "tasks.csv", csv.data))
        self.assertEqual(channel.uploads[0][3]["parse"], [])
        self.assertIsNot(channel.streams[0], channel.streams[1])
        self.assertTrue(all(stream.closed for stream in channel.streams))

    async def test_delivery_loop_backs_off_while_discord_is_not_ready(self) -> None:
        client = DisconnectedAdapterClient(FakeChannel())
        client._orchestrator = FakeOrchestrator(part())

        with patch("app.discord_bot.asyncio.sleep", new=AsyncMock()) as sleep:
            await client._delivery_loop()

        sleep.assert_awaited_once_with(2)

    async def test_allowed_event_becomes_neutral_message_and_flushes_its_conversation(self) -> None:
        channel = FakeChannel()
        client = AdapterClient(channel)
        orchestrator = FakeOrchestrator(part())
        client._orchestrator = orchestrator  # adapter dependency injection
        message = SimpleNamespace(
            id=321,
            author=SimpleNamespace(id=200, bot=False),
            channel=channel,
            content="haz el cambio en repo capnet-next-lambda-tasks",
        )

        await client.on_message(message)

        self.assertEqual(len(orchestrator.handled), 1)
        inbound = orchestrator.handled[0]
        self.assertEqual(inbound.message_id, 321)
        self.assertEqual(inbound.channel_id, 100)
        self.assertEqual(inbound.user_id, 200)
        self.assertEqual(inbound.text, message.content)
        self.assertEqual(orchestrator.keys, [inbound.conversation_key])
        self.assertEqual(channel.sent, ["respuesta"])

    async def test_outbox_delivery_fetches_uncached_channel_and_returns_message_id(self) -> None:
        channel = FakeChannel()
        client = AdapterClient(channel)
        client.use_cache = False

        sent = await client._send_outbox_part(part("fragmento"))

        self.assertEqual(sent.id, 901)
        self.assertEqual(client.fetches, [100])
        self.assertEqual(channel.sent, ["fragmento"])

    async def test_processing_error_is_delivered_through_durable_outbox(self) -> None:
        channel = FakeChannel()
        client = AdapterClient(channel)
        orchestrator = FakeOrchestrator(part())
        orchestrator.handle_error = RuntimeError("boom")
        client._orchestrator = orchestrator
        message = SimpleNamespace(
            id=322,
            author=SimpleNamespace(id=200, bot=False),
            channel=channel,
            content="consulta",
        )

        await client.on_message(message)

        self.assertEqual(len(orchestrator.failure_calls), 1)
        self.assertEqual(orchestrator.keys, [InboundMessage(322, 100, 200, "consulta").conversation_key])
        self.assertEqual(channel.sent, [PROCESSING_FAILED_MESSAGE])

    async def test_direct_error_is_only_last_resort_when_outbox_is_unavailable(self) -> None:
        channel = FakeChannel()
        client = AdapterClient(channel)
        orchestrator = FakeOrchestrator(part())
        orchestrator.handle_error = RuntimeError("handler failed")
        orchestrator.failure_error = RuntimeError("storage failed")
        client._orchestrator = orchestrator
        message = SimpleNamespace(
            id=323,
            author=SimpleNamespace(id=200, bot=False),
            channel=channel,
            content="consulta",
        )

        await client.on_message(message)

        self.assertEqual(len(orchestrator.failure_calls), 1)
        self.assertEqual(orchestrator.keys, [])
        self.assertEqual(channel.sent, [PROCESSING_FAILED_MESSAGE])

    async def test_late_worker_start_refreshes_repository_inventory(self) -> None:
        client = AdapterClient(FakeChannel())
        orchestrator = FakeOrchestrator(part())
        orchestrator.repositories = ()
        client._orchestrator = orchestrator
        client._worker = FakeInventoryWorker()

        refreshed = await client._refresh_worker_repositories(force=True)

        self.assertTrue(refreshed)
        self.assertEqual(
            orchestrator.repositories,
            ("brain-capnet", "capnet-next-lambda-tasks"),
        )

    async def test_long_lived_adapter_prunes_storage_only_when_due(self) -> None:
        client = AdapterClient(FakeChannel())
        storage = FakePrunableStorage()
        client._storage = storage  # adapter dependency injection
        client._next_storage_prune = 0

        self.assertTrue(client._prune_storage_if_due())
        self.assertFalse(client._prune_storage_if_due())
        self.assertEqual(
            storage.calls,
            [
                {
                    "memory_retention_seconds": 7 * 86_400,
                    "operational_retention_seconds": 30 * 86_400,
                }
            ],
        )
