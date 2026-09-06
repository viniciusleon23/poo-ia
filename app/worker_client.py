"""Authenticated asynchronous client for the host-side Poo-IA worker."""

from __future__ import annotations

import asyncio
from typing import Any

import aiohttp


class WorkerError(RuntimeError):
    """A recoverable problem while communicating with the local worker."""


class WorkerAuthenticationError(WorkerError):
    """The internal worker rejected its configured credentials."""


class WorkerConflictError(WorkerError):
    """The worker rejected an operation because of its current state/input."""


class WorkerNotFoundError(WorkerError):
    """The requested durable worker job does not exist."""


class WorkerUnavailableError(WorkerError):
    """The local worker cannot currently be reached."""


class WorkerClient:
    """Call the loopback-only worker without exposing its shared password."""

    _VALID_STATUSES = {
        "queued",
        "running",
        "prepared",
        "publishing",
        "succeeded",
        "failed",
        "cancelled",
    }

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        base_url: str,
        username: str,
        password: str,
    ) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._headers = {
            "Authorization": aiohttp.encode_basic_auth(username, password)
        }

    async def health(self) -> dict[str, object]:
        """Return a validated worker health document."""
        result = await self._request_json("GET", "/healthz")
        if result.get("status") != "ok":
            raise WorkerError("The worker returned an unhealthy response.")
        return result

    async def list_repositories(self) -> tuple[str, ...]:
        """Return the worker's safe direct-child repository inventory."""
        result = await self._request_json("GET", "/v1/repositories")
        repositories = result.get("repositories")
        if not isinstance(repositories, list) or not all(
            isinstance(item, str) and item.strip() for item in repositories
        ):
            raise WorkerError("The worker returned an invalid repository inventory.")
        return tuple(repositories)

    async def create_codex_job(
        self,
        *,
        job_id: str,
        repository: str,
        prompt: str,
        preflight: str | None = None,
        policy: str | None = None,
        publish: bool = False,
    ) -> dict[str, object]:
        """Create or recover one idempotent Codex job."""
        payload: dict[str, object] = {
            "job_id": job_id,
            "repository": repository,
            "prompt": prompt,
            "publish": publish,
        }
        if preflight:
            payload["preflight"] = preflight
        if policy:
            payload["policy"] = policy
        return await self._job_request("POST", "/v1/jobs/codex", payload)

    async def get_job(self, job_id: str) -> dict[str, object]:
        """Read the current public worker manifest for a job."""
        return await self._job_request("GET", f"/v1/jobs/{job_id}")

    async def cancel_job(self, job_id: str) -> dict[str, object]:
        """Cancel one queued or active worker job."""
        return await self._job_request("POST", f"/v1/jobs/{job_id}/cancel", {})

    async def publish_job(
        self, job_id: str, *, override: bool = False
    ) -> dict[str, object]:
        """Publish a prepared job, optionally overriding its validation gate."""
        return await self._job_request(
            "POST", f"/v1/jobs/{job_id}/publish", {"override": override}
        )

    async def _job_request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        result = await self._request_json(method, path, payload)
        job = result.get("job")
        if not isinstance(job, dict):
            raise WorkerError("The worker returned an invalid job response.")
        job_id = job.get("job_id")
        status = job.get("status", job.get("state"))
        if not isinstance(job_id, str) or not job_id.strip():
            raise WorkerError("The worker returned a job without an ID.")
        if status not in self._VALID_STATUSES:
            raise WorkerError("The worker returned an unknown job status.")
        return job

    async def _request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        kwargs: dict[str, object] = {"headers": self._headers}
        if payload is not None:
            kwargs["json"] = payload
        try:
            async with self._session.request(
                method, f"{self._base_url}{path}", **kwargs
            ) as response:
                if response.status in {401, 403}:
                    raise WorkerAuthenticationError(
                        "The worker rejected its internal credentials."
                    )
                if response.status == 404:
                    raise WorkerNotFoundError("The worker job was not found.")
                if response.status == 409:
                    detail = "The worker rejected the operation because of its current state."
                    try:
                        conflict = await response.json(content_type=None)
                    except (aiohttp.ClientError, ValueError):
                        conflict = None
                    if isinstance(conflict, dict) and isinstance(conflict.get("error"), str):
                        safe = " ".join(conflict["error"].split())[:500]
                        if safe:
                            detail = safe
                    raise WorkerConflictError(detail)
                if response.status >= 400:
                    raise WorkerError(f"The worker returned HTTP {response.status}.")
                try:
                    data = await response.json(content_type=None)
                except (aiohttp.ClientError, ValueError) as error:
                    raise WorkerError("The worker returned invalid JSON.") from error
        except asyncio.TimeoutError as error:
            raise WorkerUnavailableError("Timed out while contacting the worker.") from error
        except WorkerError:
            raise
        except aiohttp.ClientError as error:
            raise WorkerUnavailableError("Could not connect to the worker.") from error

        if not isinstance(data, dict):
            raise WorkerError("The worker returned an unexpected response body.")
        return data
