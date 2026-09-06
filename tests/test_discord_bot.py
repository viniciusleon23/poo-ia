from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from app.config import Settings
import asyncio

from app.discord_bot import generate_response, should_respond_to
from app.opencode_client import OpenCodeDisabledError, OpenCodeTimeoutError


def make_settings(*, allowed_user_id: int | None = None) -> Settings:
    return Settings(
        discord_token="not-a-real-token",
        discord_channel_id=100,
        ollama_model="qwen2.5-coder:3b",
        ollama_base_url="http://127.0.0.1:11434",
        allowed_user_id=allowed_user_id,
        ollama_timeout_seconds=120,
        content_root=Path("."),
    )


def make_message(*, author_id: int = 200, is_bot: bool = False, channel_id: int = 100, content: str = "Hola"):
    return SimpleNamespace(
        author=SimpleNamespace(id=author_id, bot=is_bot),
        channel=SimpleNamespace(id=channel_id),
        content=content,
    )


class MessageFilterTests(unittest.TestCase):
    def test_accepts_human_message_in_allowed_channel(self) -> None:
        self.assertTrue(should_respond_to(make_message(), make_settings()))

    def test_ignores_every_bot_message(self) -> None:
        self.assertFalse(should_respond_to(make_message(is_bot=True), make_settings()))

    def test_ignores_other_channels_and_empty_messages(self) -> None:
        settings = make_settings()
        self.assertFalse(should_respond_to(make_message(channel_id=999), settings))
        self.assertFalse(should_respond_to(make_message(content="  "), settings))

    def test_optional_owner_filter_is_enforced(self) -> None:
        settings = make_settings(allowed_user_id=777)
        self.assertFalse(should_respond_to(make_message(author_id=200), settings))
        self.assertTrue(should_respond_to(make_message(author_id=777), settings))


class FakeOllama:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return "respuesta local"


class FakeOpenCode:
    def __init__(self, *, delay: float = 0) -> None:
        self.prompts: list[str] = []
        self.delay = delay

    async def research(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if self.delay:
            await asyncio.sleep(self.delay)
        return "respuesta documental"


class ResponseGenerationTests(unittest.IsolatedAsyncioTestCase):
    async def test_small_talk_uses_ollama_with_rules_and_personality(self) -> None:
        settings = make_settings()
        ollama = FakeOllama()
        opencode = FakeOpenCode()

        result = await generate_response(
            "hola",
            settings,
            ollama=ollama,
            opencode=opencode,
            opencode_semaphore=asyncio.Semaphore(1),
        )

        self.assertEqual(result, "respuesta local")
        self.assertIn("## Mensaje del usuario\n\nhola", ollama.prompts[0])
        self.assertEqual(opencode.prompts, [])

    async def test_technical_message_uses_opencode_without_rewriting_it(self) -> None:
        settings = make_settings()
        settings = replace(settings, opencode_enabled=True)
        ollama = FakeOllama()
        opencode = FakeOpenCode()

        result = await generate_response(
            "consulta customer-service",
            settings,
            ollama=ollama,
            opencode=opencode,
            opencode_semaphore=asyncio.Semaphore(1),
        )

        self.assertEqual(result, "respuesta documental")
        self.assertEqual(opencode.prompts, ["consulta customer-service"])
        self.assertEqual(ollama.prompts, [])

    async def test_disabled_opencode_never_falls_back_to_ollama(self) -> None:
        ollama = FakeOllama()

        with self.assertRaises(OpenCodeDisabledError):
            await generate_response(
                "consulta técnica",
                make_settings(),
                ollama=ollama,
                opencode=None,
                opencode_semaphore=asyncio.Semaphore(1),
            )

        self.assertEqual(ollama.prompts, [])

    async def test_total_opencode_timeout_includes_queue_and_research(self) -> None:
        settings = make_settings()
        settings = replace(
            settings, opencode_enabled=True, opencode_timeout_seconds=0.001
        )

        with self.assertRaises(OpenCodeTimeoutError):
            await generate_response(
                "consulta técnica",
                settings,
                ollama=FakeOllama(),
                opencode=FakeOpenCode(delay=0.05),
                opencode_semaphore=asyncio.Semaphore(1),
            )
