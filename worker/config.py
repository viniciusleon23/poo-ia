"""Validated host-only configuration for the Poo-IA worker."""

from __future__ import annotations

import ipaddress
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path


class WorkerConfigurationError(ValueError):
    """Raised when the worker cannot be started safely."""


DEFAULT_OPERATIONAL_RETENTION_DAYS = 30
DEFAULT_RETENTION_SWEEP_SECONDS = 3600.0


def _boolean(environment: Mapping[str, str], name: str, default: bool = False) -> bool:
    value = str(environment.get(name, "")).strip().casefold()
    if not value:
        return default
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise WorkerConfigurationError(f"{name} must be true or false.")


def _aws_name(environment: Mapping[str, str], name: str, default: str) -> str:
    value = _value(environment, name, default)
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", value):
        raise WorkerConfigurationError(f"{name} contains invalid characters.")
    return value


def _value(environment: Mapping[str, str], name: str, default: str | None = None) -> str:
    raw = environment.get(name, default)
    value = "" if raw is None else str(raw).strip()
    if not value:
        raise WorkerConfigurationError(f"{name} must be set.")
    return value


def _absolute_path(environment: Mapping[str, str], name: str, default: str) -> Path:
    value = Path(_value(environment, name, default)).expanduser()
    if not value.is_absolute():
        raise WorkerConfigurationError(f"{name} must be an absolute path.")
    return value.resolve(strict=False)


def _validate_disjoint_roots(paths: Mapping[str, Path]) -> None:
    items = tuple(paths.items())
    for index, (first_name, first) in enumerate(items):
        for second_name, second in items[index + 1 :]:
            if (
                first == second
                or first.is_relative_to(second)
                or second.is_relative_to(first)
            ):
                raise WorkerConfigurationError(
                    f"{first_name} and {second_name} must be disjoint paths."
                )


def _positive_integer(environment: Mapping[str, str], name: str, default: int) -> int:
    value = _value(environment, name, str(default))
    if not value.isdecimal() or int(value) <= 0:
        raise WorkerConfigurationError(f"{name} must be a positive integer.")
    return int(value)


def _positive_seconds(environment: Mapping[str, str], name: str, default: float) -> float:
    value = _value(environment, name, str(default))
    try:
        parsed = float(value)
    except ValueError as error:
        raise WorkerConfigurationError(f"{name} must be a positive number of seconds.") from error
    if not math.isfinite(parsed) or parsed <= 0:
        raise WorkerConfigurationError(f"{name} must be a positive number of seconds.")
    return parsed


def _loopback_host(environment: Mapping[str, str]) -> str:
    host = _value(environment, "WORKER_HOST", "127.0.0.1")
    if host.casefold() == "localhost":
        return host
    try:
        address = ipaddress.ip_address(host)
    except ValueError as error:
        raise WorkerConfigurationError("WORKER_HOST must be a loopback address.") from error
    if not address.is_loopback:
        raise WorkerConfigurationError("WORKER_HOST must be a loopback address.")
    return host


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    """Configuration for the loopback API and local execution tools."""

    host: str
    port: int
    username: str
    password: str = field(repr=False)
    workspace: Path = Path("/home/poo/capnet-workspace")
    worktrees_root: Path = Path("/home/poo/capnet-worktrees")
    data_root: Path = Path("/home/poo/.local/share/poo-ia-worker")
    codex_executable: str = "codex"
    git_executable: str = "git"
    gh_executable: str = "gh"
    max_changed_files: int = 5
    max_changed_lines: int = 400
    codex_timeout_seconds: float = 1800.0
    validation_timeout_seconds: float = 600.0
    github_timeout_seconds: float = 120.0
    poll_interval_seconds: float = 0.5
    operational_retention_days: int = DEFAULT_OPERATIONAL_RETENTION_DAYS
    retention_sweep_seconds: float = DEFAULT_RETENTION_SWEEP_SECONDS
    validation_enabled: bool = False
    docker_executable: str = "docker"
    validation_build_timeout_seconds: float = 600.0
    aws_enabled: bool = False
    aws_executable: str = "aws"
    aws_profile: str = "default"
    aws_region: str = "us-east-1"
    aws_query_timeout_seconds: float = 20.0

    @property
    def jobs_root(self) -> Path:
        return self.data_root / "jobs"

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> "WorkerSettings":
        source = os.environ if environment is None else environment
        port = _positive_integer(source, "WORKER_PORT", 4097)
        if port > 65535:
            raise WorkerConfigurationError("WORKER_PORT must be between 1 and 65535.")
        username = _value(source, "WORKER_USERNAME", "poo-ia")
        if ":" in username or any(character in username for character in "\r\n"):
            raise WorkerConfigurationError("WORKER_USERNAME contains invalid characters.")
        password = _value(source, "WORKER_PASSWORD")
        if len(password) < 16:
            raise WorkerConfigurationError("WORKER_PASSWORD must contain at least 16 characters.")

        workspace = _absolute_path(
            source, "CAPNET_WORKSPACE", "/home/poo/capnet-workspace"
        )
        worktrees_root = _absolute_path(
            source, "CAPNET_WORKTREES", "/home/poo/capnet-worktrees"
        )
        data_root = _absolute_path(
            source,
            "WORKER_DATA_ROOT",
            "/home/poo/.local/share/poo-ia-worker",
        )
        _validate_disjoint_roots(
            {
                "CAPNET_WORKSPACE": workspace,
                "CAPNET_WORKTREES": worktrees_root,
                "WORKER_DATA_ROOT": data_root,
            }
        )

        return cls(
            host=_loopback_host(source),
            port=port,
            username=username,
            password=password,
            workspace=workspace,
            worktrees_root=worktrees_root,
            data_root=data_root,
            codex_executable=_value(source, "CODEX_EXECUTABLE", "codex"),
            git_executable=_value(source, "GIT_EXECUTABLE", "git"),
            gh_executable=_value(source, "GH_EXECUTABLE", "gh"),
            validation_enabled=_boolean(source, "VALIDATION_ENABLED"),
            docker_executable=_value(source, "DOCKER_EXECUTABLE", "docker"),
            validation_build_timeout_seconds=_positive_seconds(
                source, "VALIDATION_BUILD_TIMEOUT_SECONDS", 600.0
            ),
            aws_enabled=_boolean(source, "AWS_ENABLED"),
            aws_executable=_value(source, "AWS_CLI_EXECUTABLE", "aws"),
            aws_profile=_aws_name(source, "AWS_PROFILE", "default"),
            aws_region=_aws_name(source, "AWS_REGION", "us-east-1"),
            aws_query_timeout_seconds=_positive_seconds(
                source, "AWS_QUERY_TIMEOUT_SECONDS", 20.0
            ),
            max_changed_files=_positive_integer(source, "CODEX_MAX_CHANGED_FILES", 5),
            max_changed_lines=_positive_integer(source, "CODEX_MAX_CHANGED_LINES", 400),
            codex_timeout_seconds=_positive_seconds(source, "CODEX_TIMEOUT_SECONDS", 1800.0),
            validation_timeout_seconds=_positive_seconds(
                source, "VALIDATION_TIMEOUT_SECONDS", 600.0
            ),
            github_timeout_seconds=_positive_seconds(
                source, "GITHUB_TIMEOUT_SECONDS", 120.0
            ),
            poll_interval_seconds=_positive_seconds(source, "WORKER_POLL_SECONDS", 0.5),
            operational_retention_days=_positive_integer(
                source,
                "OPERATIONAL_RETENTION_DAYS",
                DEFAULT_OPERATIONAL_RETENTION_DAYS,
            ),
            retention_sweep_seconds=_positive_seconds(
                source,
                "WORKER_RETENTION_SWEEP_SECONDS",
                DEFAULT_RETENTION_SWEEP_SECONDS,
            ),
        )
