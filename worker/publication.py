"""Private durable intent for an asynchronous GitHub publication attempt."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .models import JOB_ID_PATTERN


PUBLICATION_FILE = "publication.json"


def publication_path(jobs_root: Path, job_id: str) -> Path:
    if not JOB_ID_PATTERN.fullmatch(job_id):
        raise ValueError("job_id has an invalid format")
    return jobs_root / job_id / PUBLICATION_FILE


def _write_publication_request(
    jobs_root: Path, job_id: str, *, override: bool, pending: bool
) -> Path:
    destination = publication_path(jobs_root, job_id)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".publication.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(
                {"override": override, "pending": pending},
                stream,
                separators=(",", ":"),
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        directory_descriptor = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if temporary.exists():
            temporary.unlink()
    return destination


def save_publication_request(jobs_root: Path, job_id: str, *, override: bool) -> Path:
    """Persist a publication request before its manifest is claimed."""
    return _write_publication_request(
        jobs_root, job_id, override=override, pending=True
    )


def _load_publication_request(
    jobs_root: Path, job_id: str
) -> tuple[bool, bool] | None:
    path = publication_path(jobs_root, job_id)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("override"), bool):
        return None
    pending = payload.get("pending", True)
    if not isinstance(pending, bool):
        return None
    return payload["override"], pending


def load_publication_override(jobs_root: Path, job_id: str) -> bool | None:
    """Return the durable override option, if a publication was requested."""
    request = _load_publication_request(jobs_root, job_id)
    return None if request is None else request[0]


def publication_is_pending(jobs_root: Path, job_id: str) -> bool:
    request = _load_publication_request(jobs_root, job_id)
    return request is not None and request[1]


def finish_publication_request(jobs_root: Path, job_id: str) -> None:
    """Prevent a normal prepared failure from being retried without new approval."""
    request = _load_publication_request(jobs_root, job_id)
    if request is None:
        return
    override, _pending = request
    _write_publication_request(
        jobs_root, job_id, override=override, pending=False
    )
