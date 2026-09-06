"""Environment-backed configuration for Poo-IA."""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


class ConfigurationError(ValueError):
    """Raised when the bot cannot start safely from its environment."""


DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_OLLAMA_MODEL = "qwen2.5-coder:3b"
DEFAULT_OLLAMA_TIMEOUT_SECONDS = 120.0


def _value(environment: Mapping[str, str], name: str, default: str | None = None) -> str:
    raw_value = environment.get(name, default)
    value = "" if raw_value is None else str(raw_value).strip()
    if not value:
        raise ConfigurationError(f"{name} must be set.")
    return value


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


def _positive_seconds(environment: Mapping[str, str], name: str) -> float:
    value = _value(environment, name, str(DEFAULT_OLLAMA_TIMEOUT_SECONDS))
    try:
        parsed = float(value)
    except ValueError as error:
        raise ConfigurationError(f"{name} must be a positive number of seconds.") from error

    if not math.isfinite(parsed) or parsed <= 0:
        raise ConfigurationError(f"{name} must be a positive number of seconds.")
    return parsed


def _ollama_url(environment: Mapping[str, str]) -> str:
    base_url = _value(environment, "OLLAMA_BASE_URL", DEFAULT_OLLAMA_BASE_URL).rstrip("/")
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigurationError("OLLAMA_BASE_URL must be an absolute HTTP(S) URL.")
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

        return cls(
            discord_token=_value(source, "DISCORD_TOKEN"),
            discord_channel_id=_positive_integer(source, "DISCORD_CHANNEL_ID", required=True),
            ollama_model=_value(source, "OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL),
            ollama_base_url=_ollama_url(source),
            allowed_user_id=_positive_integer(source, "ALLOWED_USER_ID", required=False),
            ollama_timeout_seconds=_positive_seconds(source, "OLLAMA_TIMEOUT_SECONDS"),
            content_root=root,
        )
