"""Authenticated asynchronous client for the host-side Poo-IA worker."""

from __future__ import annotations

import asyncio
import base64
import binascii
import re
from datetime import date
from typing import Any

import aiohttp

from .models import CsvAttachment


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

    async def query_aws(self, action: str, *, table: str | None = None,
                        log_group: str | None = None, output_format: str = "text",
                        business_query: dict[str, object] | None = None) -> dict[str, object]:
        """Request one fixed read operation; credentials stay on the host."""
        if output_format not in {"text", "csv"}:
            raise ValueError("Unsupported AWS output format")
        payload: dict[str, object] = {"action": action}
        if output_format == "csv":
            payload["format"] = "csv"
        if table is not None:
            payload["table"] = table
        if log_group is not None:
            payload["log_group"] = log_group
        if business_query is not None:
            if action != "count-planned-tasks" or not isinstance(business_query, dict):
                raise ValueError("Unsupported business query")
            if table is not None or log_group is not None:
                raise ValueError("Business queries use host-configured resources")
            payload["business_query"] = business_query
        response = await self._request_json("POST", "/v1/aws/query", payload)
        report = response.get("result")
        if (
            not isinstance(report, dict)
            or report.get("state") not in {"succeeded", "failed", "disabled"}
            or not isinstance(report.get("message"), str)
            or not report["message"].strip()
            or len(report["message"]) > 16_000
        ):
            raise WorkerError("El worker devolvió una respuesta AWS inválida.")
        if action == "count-planned-tasks" and report["state"] == "succeeded":
            count, pages = report.get("count"), report.get("pages")
            day, zone = report.get("date"), report.get("time_zone")
            valid_count = isinstance(count, int) and not isinstance(count, bool) and 0 <= count <= 50_000
            valid_pages = isinstance(pages, int) and not isinstance(pages, bool) and 1 <= pages <= 100
            valid_day = isinstance(day, str) and bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", day))
            if valid_day:
                try:
                    date.fromisoformat(day)
                except ValueError:
                    valid_day = False
            valid_zone = isinstance(zone, str) and 1 <= len(zone) <= 128 and bool(
                re.fullmatch(r"[A-Za-z0-9._+-]+(?:/[A-Za-z0-9._+-]+)*", zone)
            )
            if (report.get("action") != action or report.get("complete") is not True
                    or not valid_count or not valid_pages or not valid_day or not valid_zone):
                raise WorkerError("El worker no confirmó un conteo completo de tareas.")
        # Only validated public fields cross into the durable delivery layer.
        result: dict[str, object] = {"state": report["state"], "message": report["message"]}
        raw_attachment = report.get("attachment")
        expects_attachment = output_format == "csv" and report["state"] == "succeeded"
        if expects_attachment:
            try:
                if not isinstance(raw_attachment, dict):
                    raise ValueError("Missing CSV attachment")
                encoded = raw_attachment.get("content_base64")
                if not isinstance(encoded, str) or len(encoded) > 4 * ((128 * 1024 + 2) // 3):
                    raise ValueError("Invalid CSV size")
                attachment = CsvAttachment(
                    filename=raw_attachment.get("filename"),
                    data=base64.b64decode(encoded, validate=True),
                    content_type=raw_attachment.get("content_type"),
                )
                if raw_attachment.get("sha256") != attachment.sha256:
                    raise ValueError("CSV integrity mismatch")
                attachment.data.decode("utf-8-sig")
            except (ValueError, TypeError, binascii.Error) as error:
                raise WorkerError("El worker devolvió un archivo CSV inválido.") from error
            result["attachment"] = attachment
        elif raw_attachment is not None:
            raise WorkerError("El worker devolvió un archivo CSV inesperado.")
        return result

    async def create_codex_job(
        self,
        *,
        job_id: str,
        repository: str,
        prompt: str,
        preflight: str | None = None,
        policy: str | None = None,
        publish: bool = False,
        target_files: tuple[str, ...] = (),
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
        if target_files:
            payload["target_files"] = list(target_files)
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
