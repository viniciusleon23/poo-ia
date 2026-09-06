"""Authenticated loopback HTTP API for host-side jobs."""

from __future__ import annotations

import base64
import binascii
import hmac
from typing import Any

from aiohttp import web

from .config import WorkerSettings
from .github import PublicationError
from .manager import JobManager
from .models import JobRequest
from .store import (
    IdempotencyConflictError,
    ManifestNotFoundError,
    ManifestStateError,
)


MANAGER_KEY: web.AppKey[JobManager] = web.AppKey("manager", JobManager)
SETTINGS_KEY: web.AppKey[WorkerSettings] = web.AppKey("settings", WorkerSettings)


def _error(message: str, *, status: int) -> web.Response:
    return web.json_response({"error": message}, status=status)


@web.middleware
async def error_middleware(request: web.Request, handler):
    try:
        return await handler(request)
    except ManifestNotFoundError:
        return _error("job not found", status=404)
    except IdempotencyConflictError as error:
        return _error(str(error), status=409)
    except (ManifestStateError, PublicationError) as error:
        return _error(str(error), status=409)
    except (ValueError, TypeError, web.HTTPBadRequest) as error:
        message = str(error) if str(error) else "invalid request"
        return _error(message, status=400)


@web.middleware
async def authentication_middleware(request: web.Request, handler):
    settings = request.app[SETTINGS_KEY]
    header = request.headers.get("Authorization", "")
    supplied_username = ""
    supplied_password = ""
    try:
        scheme, encoded = header.split(" ", 1)
        if scheme.casefold() != "basic":
            raise ValueError
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
        supplied_username, separator, supplied_password = decoded.partition(":")
        if not separator:
            raise ValueError
    except (ValueError, UnicodeDecodeError, binascii.Error):
        pass
    username_valid = hmac.compare_digest(supplied_username, settings.username)
    password_valid = hmac.compare_digest(supplied_password, settings.password)
    valid = bool(username_valid & password_valid)
    if not valid:
        response = _error("authentication required", status=401)
        response.headers["WWW-Authenticate"] = 'Basic realm="poo-ia-worker"'
        return response
    return await handler(request)


async def _json_object(request: web.Request, *, allow_empty: bool = False) -> dict[str, Any]:
    if allow_empty and not request.can_read_body:
        return {}
    try:
        payload = await request.json()
    except Exception as error:
        if allow_empty and not request.content_length:
            return {}
        raise web.HTTPBadRequest(reason="request body must be JSON") from error
    if not isinstance(payload, dict):
        raise web.HTTPBadRequest(reason="request body must be a JSON object")
    return payload


async def health(request: web.Request) -> web.Response:
    return web.json_response(request.app[MANAGER_KEY].status())


async def create_codex_job(request: web.Request) -> web.Response:
    payload = await _json_object(request)
    allowed = {"job_id", "repository", "prompt", "preflight", "policy", "publish", "target_files"}
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ValueError(f"unknown fields: {', '.join(unknown)}")
    for required in ("job_id", "repository", "prompt"):
        if not isinstance(payload.get(required), str):
            raise ValueError(f"{required} must be a string")
    if "preflight" in payload and not isinstance(payload["preflight"], str):
        raise ValueError("preflight must be a string")
    if "policy" in payload and not isinstance(payload["policy"], str):
        raise ValueError("policy must be a string")
    publish = payload.get("publish", False)
    if not isinstance(publish, bool):
        raise ValueError("publish must be true or false")
    target_files = payload.get("target_files", [])
    if not isinstance(target_files, list):
        raise ValueError("target_files must be a list")
    job = JobRequest(
        job_id=payload["job_id"],
        repository=payload["repository"],
        prompt=payload["prompt"],
        preflight=payload.get("preflight", ""),
        policy=payload.get("policy", ""),
        publish=publish,
        target_files=tuple(target_files),
    )
    manifest, created = await request.app[MANAGER_KEY].create(job)
    return web.json_response(
        {"job": manifest.to_public_dict()}, status=202 if created else 200
    )


async def list_repositories(request: web.Request) -> web.Response:
    names = await request.app[MANAGER_KEY].repository_names()
    return web.json_response({"repositories": list(names)})


async def get_job(request: web.Request) -> web.Response:
    manifest = await request.app[MANAGER_KEY].get(request.match_info["job_id"])
    return web.json_response({"job": manifest.to_public_dict()})


async def cancel_job(request: web.Request) -> web.Response:
    manifest = await request.app[MANAGER_KEY].cancel(request.match_info["job_id"])
    return web.json_response({"job": manifest.to_public_dict()})


async def publish_job(request: web.Request) -> web.Response:
    payload = await _json_object(request, allow_empty=True)
    unknown = sorted(set(payload) - {"override"})
    if unknown:
        raise ValueError(f"unknown fields: {', '.join(unknown)}")
    override = payload.get("override", False)
    if not isinstance(override, bool):
        raise ValueError("override must be true or false")
    manifest, created = await request.app[MANAGER_KEY].publish(
        request.match_info["job_id"], override=override
    )
    return web.json_response(
        {"job": manifest.to_public_dict()}, status=202 if created else 200
    )


def create_app(
    settings: WorkerSettings,
    *,
    manager: JobManager | None = None,
) -> web.Application:
    application = web.Application(
        middlewares=(error_middleware, authentication_middleware),
        client_max_size=256 * 1024,
    )
    application[SETTINGS_KEY] = settings
    application[MANAGER_KEY] = manager or JobManager(settings)
    application.router.add_get("/healthz", health)
    application.router.add_get("/v1/repositories", list_repositories)
    application.router.add_post("/v1/jobs/codex", create_codex_job)
    application.router.add_get("/v1/jobs/{job_id}", get_job)
    application.router.add_post("/v1/jobs/{job_id}/cancel", cancel_job)
    application.router.add_post("/v1/jobs/{job_id}/publish", publish_job)

    async def lifecycle(app: web.Application):
        active_manager = app[MANAGER_KEY]
        await active_manager.start()
        yield
        await active_manager.close()

    application.cleanup_ctx.append(lifecycle)
    return application
