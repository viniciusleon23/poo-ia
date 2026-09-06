"""Serializable worker job models."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,79}$")


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PREPARED = "prepared"
    PUBLISHING = "publishing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATES = {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED}


class ValidationStatus(StrEnum):
    PASSED = "passed"
    UNCHANGED_FAILURE = "unchanged_failure"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"
    TIMED_OUT = "timed_out"


@dataclass(frozen=True, slots=True)
class JobRequest:
    job_id: str
    repository: str
    prompt: str
    preflight: str = ""
    publish: bool = False
    policy: str = ""

    def __post_init__(self) -> None:
        if not JOB_ID_PATTERN.fullmatch(self.job_id):
            raise ValueError("job_id has an invalid format")
        if not self.repository.strip():
            raise ValueError("repository must not be empty")
        if not self.prompt.strip():
            raise ValueError("prompt must not be empty")
        if (
            len(self.prompt) > 100_000
            or len(self.preflight) > 100_000
            or len(self.policy) > 100_000
        ):
            raise ValueError("job request text is too large")

    def canonical_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "job_id": self.job_id,
            "repository": self.repository.strip(),
            "prompt": self.prompt.strip(),
            "preflight": self.preflight.strip(),
            "publish": self.publish,
        }
        if self.policy.strip():
            payload["policy"] = self.policy.strip()
        return payload

    @property
    def payload_hash(self) -> str:
        encoded = json.dumps(
            self.canonical_payload(), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class ValidationResult:
    status: ValidationStatus
    command: tuple[str, ...] = ()
    exit_code: int | None = None
    baseline_exit_code: int | None = None
    log_path: str | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["status"] = self.status.value
        data["command"] = list(self.command)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ValidationResult":
        return cls(
            status=ValidationStatus(data["status"]),
            command=tuple(data.get("command", [])),
            exit_code=data.get("exit_code"),
            baseline_exit_code=data.get("baseline_exit_code"),
            log_path=data.get("log_path"),
            detail=data.get("detail"),
        )


@dataclass(frozen=True, slots=True)
class DiffMeasurement:
    changed_files: int = 0
    changed_lines: int = 0
    binary_files: tuple[str, ...] = ()
    over_budget: bool = False
    detail: str | None = None

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["binary_files"] = list(self.binary_files)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DiffMeasurement":
        return cls(
            changed_files=int(data.get("changed_files", 0)),
            changed_lines=int(data.get("changed_lines", 0)),
            binary_files=tuple(data.get("binary_files", [])),
            over_budget=bool(data.get("over_budget", False)),
            detail=data.get("detail"),
        )


@dataclass(frozen=True, slots=True)
class JobManifest:
    job_id: str
    payload_hash: str
    repository: str
    prompt: str
    preflight: str
    requested_publish: bool
    policy: str = ""
    state: JobState = JobState.QUEUED
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    terminal_at: str | None = None
    repo_path: str | None = None
    base_commit: str | None = None
    base_branch: str | None = None
    worktree: str | None = None
    branch: str | None = None
    process_pid: int | None = None
    run_attempts: int = 0
    codex_exit_code: int | None = None
    validation: ValidationResult | None = None
    diff: DiffMeasurement | None = None
    diff_sha256: str | None = None
    summary: str | None = None
    error: str | None = None
    pr_url: str | None = None
    result_path: str | None = None

    @classmethod
    def from_request(cls, request: JobRequest) -> "JobManifest":
        return cls(
            job_id=request.job_id,
            payload_hash=request.payload_hash,
            repository=request.repository.strip(),
            prompt=request.prompt.strip(),
            preflight=request.preflight.strip(),
            requested_publish=request.publish,
            policy=request.policy.strip(),
        )

    def evolve(self, **changes: object) -> "JobManifest":
        timestamp = changes.setdefault("updated_at", utc_now())
        next_state = changes.get("state", self.state)
        if self.state not in TERMINAL_STATES and next_state in TERMINAL_STATES:
            changes.setdefault("terminal_at", timestamp)
        return replace(self, **changes)

    def to_storage_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["state"] = self.state.value
        data["validation"] = self.validation.to_dict() if self.validation else None
        data["diff"] = self.diff.to_dict() if self.diff else None
        return data

    def to_public_dict(self) -> dict[str, object]:
        data = self.to_storage_dict()
        for key in (
            "payload_hash",
            "prompt",
            "preflight",
            "policy",
            "repo_path",
            "worktree",
            "process_pid",
            "result_path",
        ):
            data.pop(key, None)
        validation = data.get("validation")
        if isinstance(validation, dict):
            validation.pop("log_path", None)
        # The bot client uses ``status`` as the wire-level name while the
        # durable worker model calls the same value ``state``.
        data["status"] = data["state"]
        return data

    @classmethod
    def from_storage_dict(cls, data: dict[str, Any]) -> "JobManifest":
        copied = dict(data)
        copied["state"] = JobState(copied["state"])
        if copied.get("validation") is not None:
            copied["validation"] = ValidationResult.from_dict(copied["validation"])
        if copied.get("diff") is not None:
            copied["diff"] = DiffMeasurement.from_dict(copied["diff"])
        return cls(**copied)
