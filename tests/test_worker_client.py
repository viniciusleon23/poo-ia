from __future__ import annotations

import asyncio
import unittest

import aiohttp

from app.worker_client import (
    WorkerAuthenticationError,
    WorkerClient,
    WorkerConflictError,
    WorkerError,
    WorkerNotFoundError,
    WorkerUnavailableError,
)


class FakeResponse:
    def __init__(self, status: int, data: object) -> None:
        self.status = status
        self.data = data

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def json(self, **_kwargs: object) -> object:
        return self.data


class FakeSession:
    def __init__(self, response: FakeResponse | BaseException) -> None:
        self.response = response
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def request(self, method: str, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append((method, url, kwargs))
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


def make_client(session: FakeSession) -> WorkerClient:
    return WorkerClient(
        session,  # type: ignore[arg-type]
        base_url="http://127.0.0.1:4097/",
        username="internal",
        password="secret",
    )


class WorkerClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_creates_codex_job_with_basic_auth_and_preflight(self) -> None:
        session = FakeSession(
            FakeResponse(202, {"job": {"job_id": "job-1", "status": "queued"}})
        )
        client = make_client(session)

        result = await client.create_codex_job(
            job_id="job-1",
            repository="service",
            prompt="agrega campo",
            preflight="ruta relevante",
            policy="reglas confiables",
            publish=True,
        )

        self.assertEqual(result["job_id"], "job-1")
        method, url, kwargs = session.calls[0]
        self.assertEqual((method, url), ("POST", "http://127.0.0.1:4097/v1/jobs/codex"))
        self.assertEqual(
            kwargs["json"],
            {
                "job_id": "job-1",
                "repository": "service",
                "prompt": "agrega campo",
                "publish": True,
                "preflight": "ruta relevante",
                "policy": "reglas confiables",
            },
        )
        headers = kwargs["headers"]
        self.assertIsInstance(headers, dict)
        self.assertTrue(str(headers["Authorization"]).startswith("Basic "))

    async def test_reads_cancels_and_publishes_jobs(self) -> None:
        for method, action in (
            ("GET", lambda client: client.get_job("job-1")),
            ("POST", lambda client: client.cancel_job("job-1")),
            ("POST", lambda client: client.publish_job("job-1", override=True)),
        ):
            with self.subTest(method=method, action=action):
                session = FakeSession(
                    FakeResponse(
                        200, {"job": {"job_id": "job-1", "status": "prepared"}}
                    )
                )
                await action(make_client(session))
                self.assertEqual(session.calls[0][0], method)

    async def test_validates_health_and_job_response(self) -> None:
        client = make_client(FakeSession(FakeResponse(200, {"status": "ok"})))
        self.assertEqual((await client.health())["status"], "ok")

        invalid = make_client(FakeSession(FakeResponse(200, {"job": {}})))
        with self.assertRaises(WorkerError):
            await invalid.get_job("job-1")

    async def test_lists_repositories_and_accepts_legacy_state_name(self) -> None:
        inventory = make_client(
            FakeSession(FakeResponse(200, {"repositories": ["service-a", "service-b"]}))
        )
        self.assertEqual(
            await inventory.list_repositories(), ("service-a", "service-b")
        )

        legacy = make_client(
            FakeSession(
                FakeResponse(200, {"job": {"job_id": "job-1", "state": "prepared"}})
            )
        )
        self.assertEqual((await legacy.get_job("job-1"))["state"], "prepared")

    async def test_preserves_safe_conflict_detail(self) -> None:
        client = make_client(
            FakeSession(FakeResponse(409, {"error": "validation failed; override required"}))
        )
        with self.assertRaisesRegex(WorkerConflictError, "override required"):
            await client.publish_job("job-1")

    async def test_maps_auth_conflict_and_server_errors(self) -> None:
        cases = (
            (401, WorkerAuthenticationError),
            (404, WorkerNotFoundError),
            (409, WorkerConflictError),
            (500, WorkerError),
        )
        for status, error_type in cases:
            with self.subTest(status=status):
                client = make_client(FakeSession(FakeResponse(status, {})))
                with self.assertRaises(error_type):
                    await client.get_job("job-1")

    async def test_maps_timeout_and_connection_errors(self) -> None:
        for error in (asyncio.TimeoutError(), aiohttp.ClientConnectionError()):
            with self.subTest(error=type(error).__name__):
                client = make_client(FakeSession(error))
                with self.assertRaises(WorkerUnavailableError):
                    await client.get_job("job-1")
