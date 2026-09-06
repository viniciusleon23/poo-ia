"""Environment-backed configuration for Poo-IA."""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse


class ConfigurationError(ValueError):
    """Raised when the bot cannot start safely from its environment."""


DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_OLLAMA_MODEL = "qwen2.5-coder:3b"
DEFAULT_OLLAMA_TIMEOUT_SECONDS = 120.0
DEFAULT_OPENCODE_BASE_URL = "http://127.0.0.1:4096"
DEFAULT_OPENCODE_AGENT = "capnet-research"
DEFAULT_OPENCODE_TIMEOUT_SECONDS = 300.0
DEFAULT_OPENCODE_MAX_CONCURRENT = 1


def _value(environment: Mapping[str, str], name: str, default: str | None = None) -> str:
    raw_value = environment.get(name, default)
    value = "" if raw_value is None else str(raw_value).strip()
    if not value:
        raise ConfigurationError(f"{name} must be set.")
    return value


def _optional_value(environment: Mapping[str, str], name: str) -> str | None:
    raw_value = environment.get(name)
    value = "" if raw_value is None else str(raw_value).strip()
    return value or None


def _boolean(environment: Mapping[str, str], name: str, default: bool = False) -> bool:
    raw_value = environment.get(name)
    if raw_value is None or not str(raw_value).strip():
        return default

    normalized = str(raw_value).strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true or false.")


def _positive_integer(environment: Mapping[str, str], name: str, *, required: bool) -> int | None:
    raw_value = environment.get(name)
    value = "" if raw_value is None else str(raw_value).strip()

    if not value:
        if required:
            raise ConfigurationError(f"{name} must be set.")
        return None

    if not value.isdecimal():
        raise ConfigurationError(f"{name} must be a positive numeric Discord ID.")

    parsed = int(value)
    if parsed <= 0:
        raise ConfigurationError(f"{name} must be a positive numeric Discord ID.")
    return parsed


def _positive_seconds(environment: Mapping[str, str], name: str, default: float) -> float:
    value = _value(environment, name, str(default))
    try:
        parsed = float(value)
    except ValueError as error:
        raise ConfigurationError(f"{name} must be a positive number of seconds.") from error

    if not math.isfinite(parsed) or parsed <= 0:
        raise ConfigurationError(f"{name} must be a positive number of seconds.")
    return parsed


def _positive_count(environment: Mapping[str, str], name: str, default: int) -> int:
    value = _value(environment, name, str(default))
    if not value.isdecimal() or int(value) <= 0:
        raise ConfigurationError(f"{name} must be a positive integer.")
    return int(value)


def _http_url(environment: Mapping[str, str], name: str, default: str) -> str:
    base_url = _value(environment, name, default).rstrip("/")
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigurationError(f"{name} must be an absolute HTTP(S) URL.")
    return base_url


@dataclass(frozen=True, slots=True)
class Settings:
    """All configuration needed by the bot after startup validation."""

    discord_token: str
    discord_channel_id: int
    ollama_model: str
    ollama_base_url: str
    allowed_user_id: int | None
    ollama_timeout_seconds: float
    content_root: Path
    opencode_enabled: bool = False
    opencode_base_url: str = DEFAULT_OPENCODE_BASE_URL
    opencode_server_username: str = "opencode"
    opencode_server_password: str | None = field(default=None, repr=False)
    opencode_agent: str = DEFAULT_OPENCODE_AGENT
    opencode_timeout_seconds: float = DEFAULT_OPENCODE_TIMEOUT_SECONDS
    opencode_max_concurrent: int = DEFAULT_OPENCODE_MAX_CONCURRENT

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
        *,
        content_root: Path | None = None,
    ) -> "Settings":
        """Read and validate configuration without exposing secret values."""
        source = os.environ if environment is None else environment
        root = content_root or Path(__file__).resolve().parent.parent
        opencode_enabled = _boolean(source, "OPENCODE_ENABLED")
        opencode_password = _optional_value(source, "OPENCODE_SERVER_PASSWORD")
        if opencode_enabled and opencode_password is None:
            raise ConfigurationError("OPENCODE_SERVER_PASSWORD must be set when OpenCode is enabled.")

        return cls(
            discord_token=_value(source, "DISCORD_TOKEN"),
            discord_channel_id=_positive_integer(source, "DISCORD_CHANNEL_ID", required=True),
            ollama_model=_value(source, "OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL),
            ollama_base_url=_http_url(source, "OLLAMA_BASE_URL", DEFAULT_OLLAMA_BASE_URL),
            allowed_user_id=_positive_integer(source, "ALLOWED_USER_ID", required=False),
            ollama_timeout_seconds=_positive_seconds(
                source, "OLLAMA_TIMEOUT_SECONDS", DEFAULT_OLLAMA_TIMEOUT_SECONDS
            ),
            content_root=root,
            opencode_enabled=opencode_enabled,
            opencode_base_url=_http_url(
                source, "OPENCODE_BASE_URL", DEFAULT_OPENCODE_BASE_URL
            ),
            opencode_server_username=_value(
                source, "OPENCODE_SERVER_USERNAME", "opencode"
            ),
            opencode_server_password=opencode_password,
            opencode_agent=_value(source, "OPENCODE_AGENT", DEFAULT_OPENCODE_AGENT),
            opencode_timeout_seconds=_positive_seconds(
                source, "OPENCODE_TIMEOUT_SECONDS", DEFAULT_OPENCODE_TIMEOUT_SECONDS
            ),
            opencode_max_concurrent=_positive_count(
                source, "OPENCODE_MAX_CONCURRENT", DEFAULT_OPENCODE_MAX_CONCURRENT
            ),
        )
