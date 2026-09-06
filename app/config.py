"""Environment-backed configuration for Poo-IA."""

from __future__ import annotations

import math
import os
import ipaddress
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
DEFAULT_DATABASE_PATH = Path("/app/data/poo-ia.sqlite3")
DEFAULT_MEMORY_RETENTION_DAYS = 7
DEFAULT_MEMORY_MAX_EXCHANGES = 10
DEFAULT_MEMORY_MAX_CONTEXT_CHARS = 12_000
DEFAULT_OPERATIONAL_RETENTION_DAYS = 30
DEFAULT_JOB_MAX_CONCURRENT = 1
DEFAULT_WORKER_BASE_URL = "http://127.0.0.1:4097"
DEFAULT_WORKER_TIMEOUT_SECONDS = 30.0
DEFAULT_WORKER_POLL_SECONDS = 2.0
MINIMUM_INTERNAL_PASSWORD_LENGTH = 16


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


def _basic_auth_username(
    environment: Mapping[str, str], name: str, default: str
) -> str:
    value = _value(environment, name, default)
    if ":" in value or any(character in value for character in "\r\n"):
        raise ConfigurationError(f"{name} contains invalid Basic auth characters.")
    return value


def _internal_password(environment: Mapping[str, str], name: str) -> str | None:
    value = _optional_value(environment, name)
    if value is not None and len(value) < MINIMUM_INTERNAL_PASSWORD_LENGTH:
        raise ConfigurationError(
            f"{name} must contain at least {MINIMUM_INTERNAL_PASSWORD_LENGTH} characters."
        )
    return value


def _path(environment: Mapping[str, str], name: str, default: Path) -> Path:
    path = Path(_value(environment, name, str(default))).expanduser()
    if not path.is_absolute():
        raise ConfigurationError(f"{name} must be an absolute path.")
    return path


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


def _loopback_http_url(
    environment: Mapping[str, str], name: str, default: str
) -> str:
    """Accept only local HTTP endpoints so prompts and credentials stay on-host."""
    base_url = _http_url(environment, name, default)
    parsed = urlparse(base_url)
    if parsed.username is not None or parsed.password is not None:
        raise ConfigurationError(f"{name} must not contain URL credentials.")
    hostname = parsed.hostname
    try:
        port = parsed.port
    except ValueError as error:
        raise ConfigurationError(f"{name} has an invalid port.") from error
    if hostname is None or port is None:
        raise ConfigurationError(f"{name} must include a loopback host and port.")
    if hostname.casefold() != "localhost":
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError as error:
            raise ConfigurationError(f"{name} must use a loopback address.") from error
        if not address.is_loopback:
            raise ConfigurationError(f"{name} must use a loopback address.")
    return base_url


@dataclass(frozen=True, slots=True)
class Settings:
    """All configuration needed by the bot after startup validation."""

    discord_token: str = field(repr=False)
    discord_channel_id: int
    ollama_model: str
    ollama_base_url: str
    allowed_user_id: int
    ollama_timeout_seconds: float
    content_root: Path
    opencode_enabled: bool = False
    opencode_base_url: str = DEFAULT_OPENCODE_BASE_URL
    opencode_server_username: str = "opencode"
    opencode_server_password: str | None = field(default=None, repr=False)
    opencode_agent: str = DEFAULT_OPENCODE_AGENT
    opencode_timeout_seconds: float = DEFAULT_OPENCODE_TIMEOUT_SECONDS
    opencode_max_concurrent: int = DEFAULT_OPENCODE_MAX_CONCURRENT
    database_path: Path = DEFAULT_DATABASE_PATH
    memory_retention_days: int = DEFAULT_MEMORY_RETENTION_DAYS
    memory_max_exchanges: int = DEFAULT_MEMORY_MAX_EXCHANGES
    memory_max_context_chars: int = DEFAULT_MEMORY_MAX_CONTEXT_CHARS
    operational_retention_days: int = DEFAULT_OPERATIONAL_RETENTION_DAYS
    job_max_concurrent: int = DEFAULT_JOB_MAX_CONCURRENT
    worker_enabled: bool = False
    worker_base_url: str = DEFAULT_WORKER_BASE_URL
    worker_server_username: str = "poo-ia"
    worker_server_password: str | None = field(default=None, repr=False)
    worker_timeout_seconds: float = DEFAULT_WORKER_TIMEOUT_SECONDS
    worker_poll_seconds: float = DEFAULT_WORKER_POLL_SECONDS

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
        opencode_password = _internal_password(source, "OPENCODE_SERVER_PASSWORD")
        if opencode_enabled and opencode_password is None:
            raise ConfigurationError("OPENCODE_SERVER_PASSWORD must be set when OpenCode is enabled.")

        worker_enabled = _boolean(source, "WORKER_ENABLED")
        worker_password = _internal_password(source, "WORKER_SERVER_PASSWORD")
        if worker_enabled and worker_password is None:
            raise ConfigurationError("WORKER_SERVER_PASSWORD must be set when the worker is enabled.")

        if _boolean(source, "AWS_ENABLED"):
            raise ConfigurationError("AWS_ENABLED is not available in this phase; keep it false.")

        job_max_concurrent = _positive_count(
            source, "JOB_MAX_CONCURRENT", DEFAULT_JOB_MAX_CONCURRENT
        )
        if job_max_concurrent != 1:
            raise ConfigurationError("JOB_MAX_CONCURRENT must be 1 on this Beelink deployment.")

        return cls(
            discord_token=_value(source, "DISCORD_TOKEN"),
            discord_channel_id=_positive_integer(source, "DISCORD_CHANNEL_ID", required=True),
            ollama_model=_value(source, "OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL),
            ollama_base_url=_loopback_http_url(
                source, "OLLAMA_BASE_URL", DEFAULT_OLLAMA_BASE_URL
            ),
            allowed_user_id=_positive_integer(source, "ALLOWED_USER_ID", required=True),
            ollama_timeout_seconds=_positive_seconds(
                source, "OLLAMA_TIMEOUT_SECONDS", DEFAULT_OLLAMA_TIMEOUT_SECONDS
            ),
            content_root=root,
            opencode_enabled=opencode_enabled,
            opencode_base_url=_loopback_http_url(
                source, "OPENCODE_BASE_URL", DEFAULT_OPENCODE_BASE_URL
            ),
            opencode_server_username=_basic_auth_username(
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
            database_path=_path(source, "POOIA_DB_PATH", DEFAULT_DATABASE_PATH),
            memory_retention_days=_positive_count(
                source, "MEMORY_RETENTION_DAYS", DEFAULT_MEMORY_RETENTION_DAYS
            ),
            memory_max_exchanges=_positive_count(
                source, "MEMORY_MAX_EXCHANGES", DEFAULT_MEMORY_MAX_EXCHANGES
            ),
            memory_max_context_chars=_positive_count(
                source, "MEMORY_MAX_CONTEXT_CHARS", DEFAULT_MEMORY_MAX_CONTEXT_CHARS
            ),
            operational_retention_days=_positive_count(
                source,
                "OPERATIONAL_RETENTION_DAYS",
                DEFAULT_OPERATIONAL_RETENTION_DAYS,
            ),
            job_max_concurrent=job_max_concurrent,
            worker_enabled=worker_enabled,
            worker_base_url=_loopback_http_url(
                source, "WORKER_BASE_URL", DEFAULT_WORKER_BASE_URL
            ),
            worker_server_username=_basic_auth_username(
                source, "WORKER_SERVER_USERNAME", "poo-ia"
            ),
            worker_server_password=worker_password,
            worker_timeout_seconds=_positive_seconds(
                source, "WORKER_TIMEOUT_SECONDS", DEFAULT_WORKER_TIMEOUT_SECONDS
            ),
            worker_poll_seconds=_positive_seconds(
                source, "WORKER_POLL_SECONDS", DEFAULT_WORKER_POLL_SECONDS
            ),
        )
