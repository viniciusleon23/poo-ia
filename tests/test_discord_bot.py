from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

from app.config import Settings
from app.discord_bot import should_respond_to


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
