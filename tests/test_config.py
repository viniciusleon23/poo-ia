from __future__ import annotations

import unittest
from pathlib import Path

from app.config import (
    DEFAULT_OLLAMA_BASE_URL,
    DEFAULT_OLLAMA_MODEL,
    DEFAULT_OLLAMA_TIMEOUT_SECONDS,
    DEFAULT_DATABASE_PATH,
    DEFAULT_JOB_MAX_CONCURRENT,
    DEFAULT_MEMORY_MAX_CONTEXT_CHARS,
    DEFAULT_MEMORY_MAX_EXCHANGES,
    DEFAULT_MEMORY_RETENTION_DAYS,
    DEFAULT_OPERATIONAL_RETENTION_DAYS,
    DEFAULT_OPENCODE_AGENT,
    DEFAULT_OPENCODE_BASE_URL,
    DEFAULT_OPENCODE_MAX_CONCURRENT,
    DEFAULT_OPENCODE_TIMEOUT_SECONDS,
    DEFAULT_WORKER_BASE_URL,
    DEFAULT_WORKER_POLL_SECONDS,
    DEFAULT_WORKER_TIMEOUT_SECONDS,
    ConfigurationError,
    Settings,
)


class SettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = {
            "DISCORD_TOKEN": "not-a-real-token",
            "DISCORD_CHANNEL_ID": "123456789",
            "ALLOWED_USER_ID": "987654321",
        }
        self.content_root = Path("/tmp/poo-ia-content")

    def test_uses_safe_defaults(self) -> None:
        settings = Settings.from_environment(self.environment, content_root=self.content_root)

        self.assertNotIn("not-a-real-token", repr(settings))
        self.assertEqual(settings.discord_channel_id, 123456789)
        self.assertEqual(settings.ollama_model, DEFAULT_OLLAMA_MODEL)
        self.assertEqual(settings.ollama_base_url, DEFAULT_OLLAMA_BASE_URL)
        self.assertEqual(settings.ollama_timeout_seconds, DEFAULT_OLLAMA_TIMEOUT_SECONDS)
        self.assertEqual(settings.allowed_user_id, 987654321)
        self.assertEqual(settings.content_root, self.content_root)
        self.assertFalse(settings.opencode_enabled)
        self.assertEqual(settings.opencode_base_url, DEFAULT_OPENCODE_BASE_URL)
        self.assertEqual(settings.opencode_server_username, "opencode")
        self.assertIsNone(settings.opencode_server_password)
        self.assertEqual(settings.opencode_agent, DEFAULT_OPENCODE_AGENT)
        self.assertEqual(settings.opencode_timeout_seconds, DEFAULT_OPENCODE_TIMEOUT_SECONDS)
        self.assertEqual(settings.opencode_max_concurrent, DEFAULT_OPENCODE_MAX_CONCURRENT)
        self.assertEqual(settings.database_path, DEFAULT_DATABASE_PATH)
        self.assertEqual(settings.memory_retention_days, DEFAULT_MEMORY_RETENTION_DAYS)
        self.assertEqual(
            settings.operational_retention_days,
            DEFAULT_OPERATIONAL_RETENTION_DAYS,
        )
        self.assertEqual(settings.memory_max_exchanges, DEFAULT_MEMORY_MAX_EXCHANGES)
        self.assertEqual(settings.memory_max_context_chars, DEFAULT_MEMORY_MAX_CONTEXT_CHARS)
        self.assertEqual(settings.job_max_concurrent, DEFAULT_JOB_MAX_CONCURRENT)
        self.assertFalse(settings.worker_enabled)
        self.assertFalse(settings.aws_enabled)
        self.assertEqual(settings.worker_base_url, DEFAULT_WORKER_BASE_URL)
        self.assertEqual(settings.worker_timeout_seconds, DEFAULT_WORKER_TIMEOUT_SECONDS)
        self.assertEqual(settings.worker_poll_seconds, DEFAULT_WORKER_POLL_SECONDS)

    def test_loads_required_owner_filter(self) -> None:
        environment = {**self.environment, "ALLOWED_USER_ID": "987654321"}

        settings = Settings.from_environment(environment, content_root=self.content_root)

        self.assertEqual(settings.allowed_user_id, 987654321)

    def test_requires_token_and_channel(self) -> None:
        for missing_name in ("DISCORD_TOKEN", "DISCORD_CHANNEL_ID", "ALLOWED_USER_ID"):
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

        with self.assertRaisesRegex(ConfigurationError, "OLLAMA_BASE_URL"):
            Settings.from_environment(
                {**self.environment, "OLLAMA_BASE_URL": "http://example.com:11434"}
            )

    def test_enables_opencode_with_basic_auth(self) -> None:
        settings = Settings.from_environment(
            {
                **self.environment,
                "OPENCODE_ENABLED": "true",
                "OPENCODE_SERVER_PASSWORD": "local-secret-1234",
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
        self.assertEqual(settings.opencode_server_password, "local-secret-1234")
        self.assertEqual(settings.opencode_agent, "research")
        self.assertEqual(settings.opencode_timeout_seconds, 45)
        self.assertEqual(settings.opencode_max_concurrent, 2)
        self.assertNotIn("local-secret-1234", repr(settings))

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

    def test_rejects_weak_internal_passwords_and_invalid_usernames(self) -> None:
        cases = (
            ("OPENCODE_SERVER_PASSWORD", "too-short"),
            ("WORKER_SERVER_PASSWORD", "too-short"),
            ("OPENCODE_SERVER_USERNAME", "bad:user"),
            ("WORKER_SERVER_USERNAME", "bad\nuser"),
        )
        for name, value in cases:
            with self.subTest(name=name):
                with self.assertRaisesRegex(ConfigurationError, name):
                    Settings.from_environment({**self.environment, name: value})

    def test_rejects_remote_or_credentialed_internal_worker_urls(self) -> None:
        cases = (
            ("OPENCODE_BASE_URL", "http://192.168.1.81:4096"),
            ("OPENCODE_BASE_URL", "http://user:secret@127.0.0.1:4096"),
            ("WORKER_BASE_URL", "https://worker.example.com:4097"),
            ("WORKER_BASE_URL", "http://127.0.0.1"),
        )
        for name, value in cases:
            with self.subTest(name=name, value=value):
                with self.assertRaisesRegex(ConfigurationError, name):
                    Settings.from_environment({**self.environment, name: value})

        local = Settings.from_environment(
            {
                **self.environment,
                "OLLAMA_BASE_URL": "http://[::1]:11434/",
                "OPENCODE_BASE_URL": "http://localhost:4096/",
                "WORKER_BASE_URL": "http://127.0.0.2:4097/",
            }
        )
        self.assertEqual(local.ollama_base_url, "http://[::1]:11434")
        self.assertEqual(local.opencode_base_url, "http://localhost:4096")
        self.assertEqual(local.worker_base_url, "http://127.0.0.2:4097")

    def test_loads_memory_and_worker_configuration(self) -> None:
        settings = Settings.from_environment(
            {
                **self.environment,
                "POOIA_DB_PATH": "/tmp/poo-ia/state.sqlite3",
                "MEMORY_RETENTION_DAYS": "3",
                "MEMORY_MAX_EXCHANGES": "4",
                "MEMORY_MAX_CONTEXT_CHARS": "5000",
                "OPERATIONAL_RETENTION_DAYS": "45",
                "WORKER_ENABLED": "true",
                "WORKER_BASE_URL": "http://127.0.0.1:5001/",
                "WORKER_SERVER_USERNAME": "internal",
                "WORKER_SERVER_PASSWORD": "worker-secret-123",
                "WORKER_TIMEOUT_SECONDS": "15",
                "WORKER_POLL_SECONDS": "0.5",
            }
        )

        self.assertEqual(settings.database_path, Path("/tmp/poo-ia/state.sqlite3"))
        self.assertEqual(settings.memory_retention_days, 3)
        self.assertEqual(settings.memory_max_exchanges, 4)
        self.assertEqual(settings.memory_max_context_chars, 5000)
        self.assertEqual(settings.operational_retention_days, 45)
        self.assertTrue(settings.worker_enabled)
        self.assertEqual(settings.worker_base_url, "http://127.0.0.1:5001")
        self.assertEqual(settings.worker_server_username, "internal")
        self.assertEqual(settings.worker_server_password, "worker-secret-123")
        self.assertEqual(settings.worker_timeout_seconds, 15)
        self.assertEqual(settings.worker_poll_seconds, 0.5)
        self.assertNotIn("worker-secret-123", repr(settings))

    def test_requires_worker_password_only_when_enabled(self) -> None:
        disabled = Settings.from_environment(
            {**self.environment, "WORKER_SERVER_PASSWORD": ""}
        )
        self.assertIsNone(disabled.worker_server_password)

        with self.assertRaisesRegex(ConfigurationError, "WORKER_SERVER_PASSWORD"):
            Settings.from_environment({**self.environment, "WORKER_ENABLED": "true"})

    def test_aws_requires_authenticated_worker_and_allows_activation(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "AWS_ENABLED"):
            Settings.from_environment({**self.environment, "AWS_ENABLED": "true"})

        settings = Settings.from_environment({
            **self.environment,
            "AWS_ENABLED": "true",
            "WORKER_ENABLED": "true",
            "WORKER_SERVER_PASSWORD": "worker-secret-long-enough",
        })
        self.assertTrue(settings.aws_enabled)

    def test_rejects_non_single_job_concurrency(self) -> None:

        with self.assertRaisesRegex(ConfigurationError, "JOB_MAX_CONCURRENT"):
            Settings.from_environment({**self.environment, "JOB_MAX_CONCURRENT": "2"})

    def test_rejects_invalid_memory_and_worker_values(self) -> None:
        invalid_values = (
            ("POOIA_DB_PATH", "data/poo-ia.sqlite3"),
            ("MEMORY_RETENTION_DAYS", "0"),
            ("MEMORY_MAX_EXCHANGES", "no"),
            ("MEMORY_MAX_CONTEXT_CHARS", "0"),
            ("OPERATIONAL_RETENTION_DAYS", "0"),
            ("WORKER_BASE_URL", "127.0.0.1:4097"),
            ("WORKER_TIMEOUT_SECONDS", "0"),
            ("WORKER_POLL_SECONDS", "nan"),
        )

        for name, value in invalid_values:
            with self.subTest(name=name):
                with self.assertRaisesRegex(ConfigurationError, name):
                    Settings.from_environment({**self.environment, name: value})
