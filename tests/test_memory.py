from __future__ import annotations

import asyncio
import unittest

from app.memory import MemoryStore, format_history, is_forget_request
from app.models import Backend, ConversationKey, Exchange, InboundMessage, JobKind
from app.outbox import DurableOutbox
from app.storage import SQLiteStorage


KEY = ConversationKey("discord", 100, 200)


def complete_exchange(
    storage: SQLiteStorage,
    message_id: int,
    user_text: str,
    assistant_text: str,
    timestamp: float,
    *,
    channel_id: int = 100,
    user_id: int = 200,
) -> None:
    registration = storage.register_inbound(
        InboundMessage(
            message_id,
            channel_id,
            user_id,
            user_text,
            received_at=timestamp - 1,
        ),
        backend=Backend.OPENCODE,
    )
    outbox = DurableOutbox(storage)
    envelope = outbox.enqueue(
        registration.request.request_id,
        assistant_text,
        backend=Backend.OPENCODE,
        now=timestamp - 0.5,
    )
    for part in outbox.parts(envelope.outbox_id):
        storage.acknowledge_outbox_part(
            part.outbox_id, part.part_index, f"remote-{message_id}-{part.part_index}", now=timestamp
        )


class MemoryPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.storage = SQLiteStorage(":memory:")

    def tearDown(self) -> None:
        self.storage.close()

    def test_keeps_newest_ten_completed_exchanges_in_order(self) -> None:
        for index in range(12):
            complete_exchange(
                self.storage, index + 1, f"user-{index}", f"assistant-{index}", 100 + index
            )

        snapshot = MemoryStore(self.storage, max_exchanges=10).snapshot(KEY, now=200)

        self.assertEqual(len(snapshot.exchanges), 10)
        self.assertEqual(snapshot.exchanges[0].user_text, "user-2")
        self.assertEqual(snapshot.exchanges[-1].assistant_text, "assistant-11")
        self.assertLess(snapshot.rendered.index("user-2"), snapshot.rendered.index("user-11"))

    def test_seven_day_retention_uses_controlled_clock(self) -> None:
        day = 86_400
        complete_exchange(self.storage, 20, "expired", "old", 1 * day)
        complete_exchange(self.storage, 21, "kept", "new", 8 * day)

        snapshot = MemoryStore(self.storage, retention_days=7).snapshot(
            KEY, now=8 * day + 1
        )

        self.assertEqual([exchange.user_text for exchange in snapshot.exchanges], ["kept"])
        self.assertEqual(self.storage.list_exchanges(KEY), list(snapshot.exchanges))

    def test_context_never_exceeds_character_budget(self) -> None:
        for index in range(3):
            complete_exchange(
                self.storage,
                30 + index,
                f"request-{index}-" + "u" * 200,
                f"answer-{index}-" + "a" * 200,
                100 + index,
            )

        rendered = MemoryStore(self.storage, max_context_chars=180).load_context(
            KEY, now=200
        )

        self.assertLessEqual(len(rendered), 180)
        self.assertIn("recortado", rendered)

    def test_memory_is_isolated_by_channel_and_user(self) -> None:
        complete_exchange(self.storage, 40, "owner-main", "a", 100)
        complete_exchange(
            self.storage, 41, "other-channel", "b", 100, channel_id=999
        )
        complete_exchange(self.storage, 42, "other-user", "c", 100, user_id=999)

        memory = MemoryStore(self.storage)
        self.assertIn("owner-main", memory.load_context(KEY, now=200))
        self.assertNotIn("other-channel", memory.load_context(KEY, now=200))
        self.assertNotIn("other-user", memory.load_context(KEY, now=200))

    def test_forget_clears_prompt_and_references_but_preserves_job_audit(self) -> None:
        complete_exchange(self.storage, 50, "campo anterior", "task_available", 100)
        request = self.storage.register_inbound(
            InboundMessage(51, 100, 200, "hazlo", received_at=101),
            job_kind=JobKind.CODEX,
            repository="capnet-next-lambda-tasks",
        )
        memory = MemoryStore(self.storage)

        forgotten = memory.forget(KEY, now=102)

        self.assertEqual(forgotten.rendered, "")
        self.assertEqual(forgotten.exchanges, ())
        self.assertIsNone(forgotten.conversation.active_repository)
        self.assertIsNone(forgotten.conversation.last_job_id)
        self.assertIsNotNone(self.storage.get_job(request.job.job_id))
        self.assertEqual(memory.load_context(KEY, now=103), "")

    def test_forget_phrase_is_exact_after_normalization(self) -> None:
        self.assertTrue(is_forget_request("¡Olvida la conversación!"))
        self.assertTrue(is_forget_request("olvida   la conversacion"))
        self.assertFalse(is_forget_request("por favor olvida la conversación"))
        self.assertFalse(is_forget_request("olvida la conversación anterior"))

    def test_format_history_returns_empty_for_no_completed_turns(self) -> None:
        self.assertEqual(format_history([]), "")


class ConversationLockTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_conversation_is_serialized(self) -> None:
        storage = SQLiteStorage(":memory:")
        memory = MemoryStore(storage)
        entered: list[str] = []
        first_inside = asyncio.Event()
        release_first = asyncio.Event()

        async def first() -> None:
            async with memory.lock(KEY):
                entered.append("first")
                first_inside.set()
                await release_first.wait()

        async def second() -> None:
            await first_inside.wait()
            async with memory.lock(KEY):
                entered.append("second")

        first_task = asyncio.create_task(first())
        second_task = asyncio.create_task(second())
        await first_inside.wait()
        await asyncio.sleep(0)
        self.assertEqual(entered, ["first"])
        release_first.set()
        await asyncio.gather(first_task, second_task)
        self.assertEqual(entered, ["first", "second"])
        storage.close()

    async def test_different_conversations_do_not_block_each_other(self) -> None:
        storage = SQLiteStorage(":memory:")
        memory = MemoryStore(storage)
        other_key = ConversationKey("discord", 101, 200)
        entered_other = asyncio.Event()

        async with memory.lock(KEY):
            async def enter_other() -> None:
                async with memory.lock(other_key):
                    entered_other.set()

            task = asyncio.create_task(enter_other())
            await asyncio.wait_for(entered_other.wait(), timeout=1)
            await task
        storage.close()
