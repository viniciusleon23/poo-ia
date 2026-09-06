"""Durable, cross-process-safe JSON manifest store."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .models import JOB_ID_PATTERN, TERMINAL_STATES, JobManifest, JobRequest, JobState


class ManifestNotFoundError(KeyError):
    pass


class IdempotencyConflictError(RuntimeError):
    pass


class ManifestStateError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ManifestScanFailure:
    job_id: str
    detail: str


@dataclass(frozen=True, slots=True)
class ManifestScanResult:
    manifests: tuple[JobManifest, ...]
    failures: tuple[ManifestScanFailure, ...]


class ManifestStore:
    """Persist one private JSON document per idempotent worker job."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.root.chmod(0o700)
        except OSError:
            pass
        self._lock_path = self.root / ".lock"

    def _path(self, job_id: str) -> Path:
        if not JOB_ID_PATTERN.fullmatch(job_id):
            raise ValueError("job_id has an invalid format")
        return self.root / f"{job_id}.json"

    @contextmanager
    def _locked(self):
        descriptor = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _read_unlocked(self, job_id: str) -> JobManifest:
        path = self._path(job_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise ManifestNotFoundError(job_id) from error
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
            raise RuntimeError(f"job manifest {job_id} is unreadable") from error
        return JobManifest.from_storage_dict(data)

    def _write_unlocked(self, manifest: JobManifest) -> None:
        destination = self._path(manifest.job_id)
        encoded = json.dumps(
            manifest.to_storage_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{manifest.job_id}.", suffix=".tmp", dir=self.root
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            directory_descriptor = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            if temporary.exists():
                temporary.unlink()

    def create_or_get(self, request: JobRequest) -> tuple[JobManifest, bool]:
        with self._locked():
            try:
                existing = self._read_unlocked(request.job_id)
            except ManifestNotFoundError:
                manifest = JobManifest.from_request(request)
                self._write_unlocked(manifest)
                return manifest, True
            if existing.payload_hash != request.payload_hash:
                raise IdempotencyConflictError(
                    "job_id was already used with a different payload"
                )
            return existing, False

    def get(self, job_id: str) -> JobManifest:
        with self._locked():
            return self._read_unlocked(job_id)

    def update(
        self,
        job_id: str,
        *,
        expected: Iterable[JobState] | None = None,
        transform: Callable[[JobManifest], JobManifest],
    ) -> JobManifest:
        with self._locked():
            current = self._read_unlocked(job_id)
            expected_states = set(expected) if expected is not None else None
            if expected_states is not None and current.state not in expected_states:
                wanted = ", ".join(sorted(state.value for state in expected_states))
                raise ManifestStateError(
                    f"job {job_id} is {current.state.value}; expected {wanted}"
                )
            updated = transform(current)
            if updated.job_id != current.job_id or updated.payload_hash != current.payload_hash:
                raise ManifestStateError("manifest identity cannot be changed")
            self._write_unlocked(updated)
            return updated

    def list(self) -> list[JobManifest]:
        with self._locked():
            manifests: list[JobManifest] = []
            for path in sorted(self.root.glob("*.json")):
                manifests.append(self._read_unlocked(path.stem))
            return sorted(manifests, key=lambda item: (item.created_at, item.job_id))

    def scan(self) -> ManifestScanResult:
        """Read valid manifests while isolating individual corrupt documents."""
        with self._locked():
            manifests: list[JobManifest] = []
            failures: list[ManifestScanFailure] = []
            for path in sorted(self.root.glob("*.json")):
                try:
                    manifests.append(self._read_unlocked(path.stem))
                except Exception as error:
                    detail = " ".join(str(error).split())[:500]
                    failures.append(
                        ManifestScanFailure(
                            path.stem,
                            detail or "manifest is unreadable",
                        )
                    )
            return ManifestScanResult(
                tuple(
                    sorted(
                        manifests,
                        key=lambda item: (item.created_at, item.job_id),
                    )
                ),
                tuple(failures),
            )

    def delete_if_expired_terminal(
        self,
        job_id: str,
        *,
        payload_hash: str,
        cutoff: datetime,
    ) -> bool:
        """Conditionally unlink an expired terminal manifest and fsync its root."""
        if cutoff.tzinfo is None:
            raise ValueError("cutoff must include a timezone")
        with self._locked():
            try:
                current = self._read_unlocked(job_id)
            except ManifestNotFoundError:
                return False
            if current.payload_hash != payload_hash or current.state not in TERMINAL_STATES:
                return False
            raw_timestamp = current.terminal_at or current.updated_at
            try:
                terminal_at = datetime.fromisoformat(raw_timestamp)
            except (TypeError, ValueError) as error:
                raise ManifestStateError(
                    f"job {job_id} has an invalid terminal timestamp"
                ) from error
            if terminal_at.tzinfo is None:
                raise ManifestStateError(
                    f"job {job_id} has a terminal timestamp without timezone"
                )
            if terminal_at.astimezone(UTC) > cutoff.astimezone(UTC):
                return False
            self._path(job_id).unlink()
            directory_descriptor = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
            return True
