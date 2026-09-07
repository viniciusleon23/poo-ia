from __future__ import annotations

import asyncio
import base64
import hashlib
import unittest

import aiohttp
from app.models import CsvAttachment

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
    async def test_business_query_uses_structured_parameters_and_public_delivery_only(self) -> None:
        session = FakeSession(FakeResponse(200, {"result": {
            "state": "succeeded", "message": "17 tareas planeadas.",
            "action": "count-planned-tasks", "complete": True, "count": 17,
            "pages": 2, "date": "2026-09-07", "time_zone": "America/Mexico_City",
            "LastEvaluatedKey": "private-cursor", "Items": ["not-for-model"],
        }}))
        query = {"dealer_id": "COMAZDCALC2", "day": "tomorrow", "requested_at": 1_788_740_000.0}
        result = await make_client(session).query_aws("count-planned-tasks", business_query=query)
        self.assertEqual(session.calls[0][2]["json"], {
            "action": "count-planned-tasks", "business_query": query,
        })
        self.assertEqual(result, {"state": "succeeded", "message": "17 tareas planeadas."})
        for options in (
            {"action": "list-dynamodb", "business_query": query},
            {"action": "count-planned-tasks", "business_query": query, "table": "injected-table"},
            {"action": "count-planned-tasks", "business_query": []},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                await make_client(session).query_aws(**options)
        self.assertEqual(len(session.calls), 1)

    async def test_business_success_requires_verified_complete_count_metadata(self) -> None:
        valid = {"state": "succeeded", "message": "17 tareas planeadas.",
                 "action": "count-planned-tasks", "complete": True, "count": 17,
                 "pages": 2, "date": "2026-09-07", "time_zone": "America/Mexico_City"}
        for invalid in (
            {"complete": False}, {"complete": 1}, {"action": "scan-dynamodb"},
            {"count": None}, {"count": True}, {"count": -1}, {"count": 50_001},
            {"pages": 0}, {"pages": True}, {"pages": 101},
            {"date": "2026-02-30"}, {"time_zone": ""},
        ):
            with self.subTest(invalid=invalid):
                session = FakeSession(FakeResponse(200, {"result": valid | invalid}))
                with self.assertRaises(WorkerError):
                    await make_client(session).query_aws("count-planned-tasks", business_query={
                        "dealer_id": "COMAZDCALC2", "day": "tomorrow", "requested_at": 100.0,
                    })

    async def test_csv_transport_is_validated_and_decoded_without_extra_fields(self) -> None:
        data = b'\xef\xbb\xbftable_name\r\nTasks\r\n'
        attachment = {"filename": "dynamodb.csv", "content_type": "text/csv",
                      "content_base64": base64.b64encode(data).decode(),
                      "sha256": hashlib.sha256(data).hexdigest()}
        session = FakeSession(FakeResponse(200, {"result": {
            "state": "succeeded", "message": "CSV adjunto", "attachment": attachment,
            "internal": "not-for-delivery",
        }}))
        result = await make_client(session).query_aws("list-dynamodb", output_format="csv")
        self.assertEqual(session.calls[0][2]["json"], {"action": "list-dynamodb", "format": "csv"})
        self.assertEqual(result["attachment"], CsvAttachment("dynamodb.csv", data))
        self.assertNotIn("internal", result)

    async def test_invalid_csv_never_reaches_delivery(self) -> None:
        data = b"field\r\nvalue\r\n"
        valid = {"filename": "query.csv", "content_type": "text/csv",
                 "content_base64": base64.b64encode(data).decode(),
                 "sha256": hashlib.sha256(data).hexdigest()}
        invalid_attachments = (
            None, {}, valid | {"filename": "../secret.csv"}, valid | {"filename": 17},
            valid | {"content_type": "text/html"}, valid | {"sha256": "0" * 64},
            valid | {"content_base64": "%%%"},
            valid | {"content_base64": "a" * 174_769},
            valid | {"content_base64": base64.b64encode(b"\xff").decode(), "sha256": hashlib.sha256(b"\xff").hexdigest()},
        )
        for attachment in invalid_attachments:
            with self.subTest(attachment=type(attachment).__name__):
                session = FakeSession(FakeResponse(200, {"result": {
                    "state": "succeeded", "message": "CSV", "attachment": attachment,
                }}))
                with self.assertRaises(WorkerError):
                    await make_client(session).query_aws("list-dynamodb", output_format="csv")
        for state, output_format in (("failed", "csv"), ("succeeded", "text")):
            session = FakeSession(FakeResponse(200, {"result": {
                "state": state, "message": "AWS", "attachment": valid,
            }}))
            with self.assertRaises(WorkerError):
                await make_client(session).query_aws("list-dynamodb", output_format=output_format)

    async def test_cloudwatch_query_sends_named_group_only(self) -> None:
        session = FakeSession(FakeResponse(200, {"result": {"state": "succeeded", "message": "Logs acotados"}}))
        await make_client(session).query_aws("read-logs", log_group="/aws/lambda/tasks")
        self.assertEqual(session.calls[0][2]["json"], {"action": "read-logs", "log_group": "/aws/lambda/tasks"})
    async def test_aws_query_sends_only_action_and_table_and_returns_public_report(self) -> None:
        session = FakeSession(FakeResponse(200, {"result": {
            "state": "succeeded", "message": "Tabla Tasks activa", "unused": "private-extra",
        }}))
        report = await make_client(session).query_aws("describe-dynamodb", table="Tasks")
        self.assertEqual(report, {"state": "succeeded", "message": "Tabla Tasks activa"})
        method, url, kwargs = session.calls[0]
        self.assertEqual((method, url), ("POST", "http://127.0.0.1:4097/v1/aws/query"))
        self.assertEqual(kwargs["json"], {"action": "describe-dynamodb", "table": "Tasks"})

    async def test_aws_query_rejects_malformed_or_unbounded_reports(self) -> None:
        for report in ({}, {"state": "succeeded", "message": {}}, {"state": "succeeded", "message": "x" * 16_001}):
            with self.subTest(report=report):
                with self.assertRaises(WorkerError):
                    await make_client(FakeSession(FakeResponse(200, {"result": report}))).query_aws("identity")
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
