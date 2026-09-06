from __future__ import annotations

import unittest
from pathlib import Path

from app.config import (
    DEFAULT_OLLAMA_BASE_URL,
    DEFAULT_OLLAMA_MODEL,
    DEFAULT_OLLAMA_TIMEOUT_SECONDS,
    ConfigurationError,
    Settings,
)


class SettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = {
            "DISCORD_TOKEN": "not-a-real-token",
            "DISCORD_CHANNEL_ID": "123456789",
        }
        self.content_root = Path("/tmp/poo-ia-content")

    def test_uses_safe_defaults(self) -> None:
        settings = Settings.from_environment(self.environment, content_root=self.content_root)

        self.assertEqual(settings.discord_channel_id, 123456789)
        self.assertEqual(settings.ollama_model, DEFAULT_OLLAMA_MODEL)
        self.assertEqual(settings.ollama_base_url, DEFAULT_OLLAMA_BASE_URL)
        self.assertEqual(settings.ollama_timeout_seconds, DEFAULT_OLLAMA_TIMEOUT_SECONDS)
        self.assertIsNone(settings.allowed_user_id)
        self.assertEqual(settings.content_root, self.content_root)

    def test_allows_optional_owner_filter(self) -> None:
        environment = {**self.environment, "ALLOWED_USER_ID": "987654321"}

        settings = Settings.from_environment(environment, content_root=self.content_root)

        self.assertEqual(settings.allowed_user_id, 987654321)

    def test_requires_token_and_channel(self) -> None:
        for missing_name in ("DISCORD_TOKEN", "DISCORD_CHANNEL_ID"):
            environment = self.environment.copy()
            del environment[missing_name]

            with self.subTest(missing_name=missing_name):
                with self.assertRaisesRegex(ConfigurationError, missing_name):
                    Settings.from_environment(environment)

    def test_rejects_invalid_discord_ids(self) -> None:
        for name in ("DISCORD_CHANNEL_ID", "ALLOWED_USER_ID"):
            environment = {**self.environment, name: "not-a-number"}

            with self.subTest(name=name):
                with self.assertRaisesRegex(ConfigurationError, name):
                    Settings.from_environment(environment)

    def test_rejects_invalid_ollama_url_and_timeout(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "OLLAMA_BASE_URL"):
            Settings.from_environment({**self.environment, "OLLAMA_BASE_URL": "127.0.0.1"})

        with self.assertRaisesRegex(ConfigurationError, "OLLAMA_TIMEOUT_SECONDS"):
            Settings.from_environment({**self.environment, "OLLAMA_TIMEOUT_SECONDS": "0"})
