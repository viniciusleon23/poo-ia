from __future__ import annotations

import unittest
from pathlib import Path

from app.config import (
    DEFAULT_OLLAMA_BASE_URL,
    DEFAULT_OLLAMA_MODEL,
    DEFAULT_OLLAMA_TIMEOUT_SECONDS,
    DEFAULT_OPENCODE_AGENT,
    DEFAULT_OPENCODE_BASE_URL,
    DEFAULT_OPENCODE_MAX_CONCURRENT,
    DEFAULT_OPENCODE_TIMEOUT_SECONDS,
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
        self.assertFalse(settings.opencode_enabled)
        self.assertEqual(settings.opencode_base_url, DEFAULT_OPENCODE_BASE_URL)
        self.assertEqual(settings.opencode_server_username, "opencode")
        self.assertIsNone(settings.opencode_server_password)
        self.assertEqual(settings.opencode_agent, DEFAULT_OPENCODE_AGENT)
        self.assertEqual(settings.opencode_timeout_seconds, DEFAULT_OPENCODE_TIMEOUT_SECONDS)
        self.assertEqual(settings.opencode_max_concurrent, DEFAULT_OPENCODE_MAX_CONCURRENT)

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

    def test_enables_opencode_with_basic_auth(self) -> None:
        settings = Settings.from_environment(
            {
                **self.environment,
                "OPENCODE_ENABLED": "true",
                "OPENCODE_SERVER_PASSWORD": "local-secret",
                "OPENCODE_BASE_URL": "http://127.0.0.1:5000/",
                "OPENCODE_SERVER_USERNAME": "worker",
                "OPENCODE_AGENT": "research",
                "OPENCODE_TIMEOUT_SECONDS": "45",
                "OPENCODE_MAX_CONCURRENT": "2",
            }
        )

        self.assertTrue(settings.opencode_enabled)
        self.assertEqual(settings.opencode_base_url, "http://127.0.0.1:5000")
        self.assertEqual(settings.opencode_server_username, "worker")
        self.assertEqual(settings.opencode_server_password, "local-secret")
        self.assertEqual(settings.opencode_agent, "research")
        self.assertEqual(settings.opencode_timeout_seconds, 45)
        self.assertEqual(settings.opencode_max_concurrent, 2)
        self.assertNotIn("local-secret", repr(settings))

    def test_requires_password_only_when_opencode_is_enabled(self) -> None:
        disabled = Settings.from_environment(
            {**self.environment, "OPENCODE_SERVER_PASSWORD": ""}
        )
        self.assertIsNone(disabled.opencode_server_password)

        with self.assertRaisesRegex(ConfigurationError, "OPENCODE_SERVER_PASSWORD"):
            Settings.from_environment({**self.environment, "OPENCODE_ENABLED": "true"})

    def test_rejects_invalid_opencode_values(self) -> None:
        invalid_values = (
            ("OPENCODE_ENABLED", "sometimes"),
            ("OPENCODE_BASE_URL", "127.0.0.1:4096"),
            ("OPENCODE_TIMEOUT_SECONDS", "0"),
            ("OPENCODE_MAX_CONCURRENT", "0"),
        )

        for name, value in invalid_values:
            with self.subTest(name=name):
                with self.assertRaisesRegex(ConfigurationError, name):
                    Settings.from_environment({**self.environment, name: value})
