from __future__ import annotations

import tempfile
import unittest
import json
from dataclasses import replace
from pathlib import Path

from aiohttp import BasicAuth
from aiohttp.test_utils import TestClient, TestServer

from worker.api import create_app
from worker.aws_queries import AwsQueries
from worker.processes import CommandResult
from tests.worker.test_aws_queries import FakeRunner
from worker.config import WorkerSettings
from worker.models import JobManifest, JobRequest, JobState
from worker.store import IdempotencyConflictError, ManifestNotFoundError


class FakeManager:
    def __init__(self) -> None:
        self.jobs: dict[str, JobManifest] = {}
        self.started = False
        self.closed = False

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True

    def status(self) -> dict[str, object]:
        return {"status": "ok", "queue_depth": 0, "states": {}}

    async def create(self, request: JobRequest):
        existing = self.jobs.get(request.job_id)
        if existing:
            if existing.payload_hash != request.payload_hash:
                raise IdempotencyConflictError("payload conflict")
            return existing, False
        manifest = JobManifest.from_request(request)
        self.jobs[request.job_id] = manifest
        return manifest, True

    async def repository_names(self) -> tuple[str, ...]:
        return ("capnet-api", "capnet-tasks")

    async def get(self, job_id: str) -> JobManifest:
        try:
            return self.jobs[job_id]
        except KeyError as error:
            raise ManifestNotFoundError(job_id) from error

    async def cancel(self, job_id: str) -> JobManifest:
        current = await self.get(job_id)
        current = current.evolve(state=JobState.CANCELLED)
        self.jobs[job_id] = current
        return current

    async def publish(self, job_id: str, *, override: bool = False):
        current = await self.get(job_id)
        current = current.evolve(
            state=JobState.PUBLISHING,
        )
        self.jobs[job_id] = current
        return current, True


class WorkerApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.settings = WorkerSettings(
            host="127.0.0.1",
            port=4097,
            username="poo-ia",
            password="this-is-a-private-password",
            workspace=root / "workspace",
            worktrees_root=root / "worktrees",
            data_root=root / "data",
        )
        self.manager = FakeManager()
        self.client = TestClient(
            TestServer(create_app(self.settings, manager=self.manager))
        )
        await self.client.start_server()
        self.auth_headers = {
            "Authorization": BasicAuth(
                self.settings.username, self.settings.password
            ).encode()
        }

    async def asyncTearDown(self) -> None:
        await self.client.close()
        self.temporary.cleanup()

    async def test_every_endpoint_requires_basic_auth(self) -> None:
        unauthenticated = await self.client.get("/healthz")
        wrong = await self.client.get(
            "/healthz",
            headers={"Authorization": BasicAuth("poo-ia", "wrong-password").encode()},
        )
        accepted = await self.client.get("/healthz", headers=self.auth_headers)

        self.assertEqual(unauthenticated.status, 401)
        self.assertEqual(wrong.status, 401)
        self.assertEqual(accepted.status, 200)
        self.assertEqual((await accepted.json())["status"], "ok")

    async def test_aws_endpoint_is_authenticated_and_disabled_by_default(self) -> None:
        rejected = await self.client.post("/v1/aws/query", json={"action": "identity"})
        disabled = await self.client.post(
            "/v1/aws/query", headers=self.auth_headers, json={"action": "identity"}
        )
        self.assertEqual(rejected.status, 401)
        self.assertEqual(disabled.status, 200)
        self.assertEqual((await disabled.json())["result"]["state"], "disabled")

    async def test_aws_endpoint_accepts_only_structured_allowlisted_operation(self) -> None:
        enabled = replace(self.settings, aws_enabled=True)
        runner = FakeRunner(CommandResult(0, json.dumps({"TableNames": ["Tasks"]})))
        client = TestClient(TestServer(create_app(
            enabled, manager=FakeManager(), aws_queries=AwsQueries(enabled, runner=runner)
        )))
        await client.start_server()
        try:
            accepted = await client.post(
                "/v1/aws/query", headers=self.auth_headers, json={"action": "list-dynamodb"}
            )
            self.assertEqual(accepted.status, 200)
            self.assertIn("Tasks", (await accepted.json())["result"]["message"])
            for payload in ({"action": "delete-table"}, {"action": "identity"}, {"action": "list-lambdas"}, {"action": "list-dynamodb", "argv": ["whoami"]}, {"action": "describe-dynamodb", "table": "$(secret)"}, {"action": "read-logs", "log_group": "$(secret)"}):
                rejected = await client.post("/v1/aws/query", headers=self.auth_headers, json=payload)
                self.assertEqual(rejected.status, 400)
            self.assertEqual(len(runner.calls), 1)
        finally:
            await client.close()

    async def test_create_get_cancel_and_idempotent_retry(self) -> None:
        payload = {
            "job_id": "discord-1001",
            "repository": "capnet-tasks",
            "prompt": "Add task_available.",
            "preflight": "Target schema identified.",
            "policy": "Follow the repository and Poo-IA rules.",
            "publish": False,
        }
        created = await self.client.post(
            "/v1/jobs/codex", headers=self.auth_headers, json=payload
        )
        repeated = await self.client.post(
            "/v1/jobs/codex", headers=self.auth_headers, json=payload
        )
        fetched = await self.client.get(
            "/v1/jobs/discord-1001", headers=self.auth_headers
        )
        cancelled = await self.client.post(
            "/v1/jobs/discord-1001/cancel", headers=self.auth_headers
        )

        self.assertEqual(created.status, 202)
        self.assertEqual(repeated.status, 200)
        self.assertEqual(fetched.status, 200)
        self.assertNotIn("prompt", (await fetched.json())["job"])
        self.assertNotIn("policy", (await fetched.json())["job"])
        cancelled_job = (await cancelled.json())["job"]
        self.assertEqual(cancelled_job["state"], "cancelled")
        self.assertEqual(cancelled_job["status"], "cancelled")

    async def test_lists_only_manager_approved_repository_names(self) -> None:
        response = await self.client.get(
            "/v1/repositories", headers=self.auth_headers
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(
            await response.json(),
            {"repositories": ["capnet-api", "capnet-tasks"]},
        )

    async def test_payload_conflict_unknown_fields_and_missing_job_are_safe_errors(self) -> None:
        payload = {
            "job_id": "discord-1002",
            "repository": "repo",
            "prompt": "first",
        }
        await self.client.post(
            "/v1/jobs/codex", headers=self.auth_headers, json=payload
        )
        conflict = await self.client.post(
            "/v1/jobs/codex",
            headers=self.auth_headers,
            json=payload | {"prompt": "different"},
        )
        bad = await self.client.post(
            "/v1/jobs/codex",
            headers=self.auth_headers,
            json=payload | {"aws": "anything"},
        )
        missing = await self.client.get(
            "/v1/jobs/no-such-job", headers=self.auth_headers
        )

        self.assertEqual(conflict.status, 409)
        self.assertEqual(bad.status, 400)
        self.assertEqual(missing.status, 404)

    async def test_publish_accepts_explicit_override(self) -> None:
        await self.client.post(
            "/v1/jobs/codex",
            headers=self.auth_headers,
            json={
                "job_id": "discord-1003",
                "repository": "repo",
                "prompt": "change",
            },
        )
        response = await self.client.post(
            "/v1/jobs/discord-1003/publish",
            headers=self.auth_headers,
            json={"override": True},
        )
        body = await response.json()
        self.assertEqual(response.status, 202)
        self.assertEqual(body["job"]["state"], "publishing")


if __name__ == "__main__":
    unittest.main()
