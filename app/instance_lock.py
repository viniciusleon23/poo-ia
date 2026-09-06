"""Single-process lease for the SQLite-backed core and Discord outbox."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path


class InstanceAlreadyRunningError(RuntimeError):
    """Another core process already owns the same durable state."""


class InstanceLock:
    """Hold an advisory file lock for the lifetime of one core process."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            path,
            os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        try:
            os.fchmod(descriptor, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise InstanceAlreadyRunningError(
                    f"another Poo-IA core already owns {path}"
                ) from error
            os.ftruncate(descriptor, 0)
            os.write(descriptor, f"{os.getpid()}\n".encode("ascii"))
            os.fsync(descriptor)
        except Exception:
            os.close(descriptor)
            raise
        self.path = path
        self._descriptor: int | None = descriptor

    @classmethod
    def for_database(cls, database_path: Path) -> "InstanceLock":
        return cls(database_path.parent / f".{database_path.name}.instance.lock")

    def close(self) -> None:
        descriptor = self._descriptor
        if descriptor is None:
            return
        self._descriptor = None
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def __enter__(self) -> "InstanceLock":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
