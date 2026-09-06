from __future__ import annotations

import asyncio
import threading
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence

from worker.config import WorkerSettings
from worker.manager import JobManager
from worker.models import JobRequest, JobState
from worker.publication import finish_publication_request, save_publication_request
from worker.retention import RetentionReport
from worker.store import ManifestStateError, ManifestStore


class FakeProcesses:
    def __init__(self) -> None:
        self.next_pid = 100
        self.launched: list[tuple[tuple[str, ...], int, str]] = []
        self.alive: set[int] = set()
        self.owners: dict[int, str] = {}
        self.terminated: list[int] = []

    def launch(self, argv: Sequence[str], *, cwd: Path, log_path: Path) -> int:
        pid = self.next_pid
        self.next_pid += 1
        job_id = str(argv[-1])
        self.launched.append((tuple(argv), pid, job_id))
        self.alive.add(pid)
        self.owners[pid] = job_id
        return pid

    def is_alive(self, pid: int) -> bool:
        return pid in self.alive

    def is_job_process(self, pid: int, job_id: str) -> bool:
        return pid in self.alive and self.owners.get(pid) == job_id

    def terminate_group(self, pid: int) -> None:
        self.terminated.append(pid)
        self.alive.discard(pid)


class FakeRetention:
    def __init__(
        self,
        *,
        error: Exception | None = None,
        entered: threading.Event | None = None,
        release: threading.Event | None = None,
    ) -> None:
        self.error = error
        self.entered = entered
        self.release = release
        self.calls = 0

    def prune_once(self) -> RetentionReport:
        self.calls += 1
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            self.release.wait(timeout=1)
        if self.error is not None:
            raise self.error
        return RetentionReport(0, 0, (), (), False)


class JobManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.settings = WorkerSettings(
            host="127.0.0.1",
            port=4097,
            username="test",
            password="long-enough-test-password",
            workspace=root / "workspace",
            worktrees_root=root / "worktrees",
            data_root=root / "data",
            poll_interval_seconds=0.01,
        )
        self.store = ManifestStore(self.settings.jobs_root)
        self.processes = FakeProcesses()
        self.manager = JobManager(
            self.settings,
            store=self.store,
            processes=self.processes,
            project_root=root,
        )

    async def asyncTearDown(self) -> None:
        await self.manager.close()
        self.temporary.cleanup()

    async def wait_for(self, predicate, timeout: float = 1) -> None:
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(0.005)

    async def test_dispatches_exactly_one_heavy_job_at_a_time(self) -> None:
        await self.manager.start()
        await self.manager.create(JobRequest("discord-1101", "repo", "first"))
        await self.manager.create(JobRequest("discord-1102", "repo", "second"))
        await self.wait_for(lambda: len(self.processes.launched) == 1)
        await asyncio.sleep(0.04)
        self.assertEqual(len(self.processes.launched), 1)

        first_pid = self.processes.launched[0][1]
        self.store.update(
            "discord-1101",
            expected=(JobState.RUNNING,),
            transform=lambda item: item.evolve(
                state=JobState.PREPARED, process_pid=None
            ),
        )
        self.processes.alive.discard(first_pid)
        await self.wait_for(lambda: len(self.processes.launched) == 2)

        self.assertEqual(self.processes.launched[0][2], "discord-1101")
        self.assertEqual(self.processes.launched[1][2], "discord-1102")

    async def test_stale_or_unowned_pid_is_not_signalled(self) -> None:
        self.store.create_or_get(JobRequest("discord-1103", "repo", "change"))
        self.store.update(
            "discord-1103",
            expected=(JobState.QUEUED,),
            transform=lambda item: item.evolve(
                state=JobState.RUNNING, process_pid=999
            ),
        )
        self.processes.alive.add(999)
        self.processes.owners[999] = "some-other-job"

        cancelled = await self.manager.cancel("discord-1103")

        self.assertEqual(cancelled.state, JobState.CANCELLED)
        self.assertEqual(self.processes.terminated, [])

    async def test_cancel_signals_only_the_owned_detached_group(self) -> None:
        self.store.create_or_get(JobRequest("discord-1104", "repo", "change"))
        self.store.update(
            "discord-1104",
            expected=(JobState.QUEUED,),
            transform=lambda item: item.evolve(
                state=JobState.RUNNING, process_pid=104
            ),
        )
        self.processes.alive.add(104)
        self.processes.owners[104] = "discord-1104"

        cancelled = await self.manager.cancel("discord-1104")

        self.assertEqual(cancelled.state, JobState.CANCELLED)
        self.assertEqual(self.processes.terminated, [104])

    async def test_cancel_publication_stops_owned_group_and_preserves_prepared_change(self) -> None:
        self.store.create_or_get(JobRequest("discord-1107", "repo", "change"))
        self.store.update(
            "discord-1107",
            expected=(JobState.QUEUED,),
            transform=lambda item: item.evolve(
                state=JobState.PUBLISHING, process_pid=107
            ),
        )
        self.processes.alive.add(107)
        self.processes.owners[107] = "discord-1107"

        cancelled = await self.manager.cancel("discord-1107")

        self.assertEqual(cancelled.state, JobState.PREPARED)
        self.assertIn("cancel", (cancelled.error or "").casefold())
        self.assertEqual(self.processes.terminated, [107])

    async def test_cancel_publication_returns_pr_when_completion_wins_race(self) -> None:
        self.store.create_or_get(JobRequest("discord-1117", "repo", "change"))
        self.store.update(
            "discord-1117",
            expected=(JobState.QUEUED,),
            transform=lambda item: item.evolve(
                state=JobState.PUBLISHING, process_pid=117
            ),
        )
        self.processes.alive.add(117)
        self.processes.owners[117] = "discord-1117"
        terminate = self.processes.terminate_group

        def complete_then_terminate(pid: int) -> None:
            self.store.update(
                "discord-1117",
                expected=(JobState.PUBLISHING,),
                transform=lambda item: item.evolve(
                    state=JobState.SUCCEEDED,
                    process_pid=None,
                    pr_url="https://github.com/example/repo/pull/117",
                ),
            )
            terminate(pid)

        self.processes.terminate_group = complete_then_terminate

        result = await self.manager.cancel("discord-1117")

        self.assertEqual(result.state, JobState.SUCCEEDED)
        self.assertEqual(result.pr_url, "https://github.com/example/repo/pull/117")
        self.assertEqual(self.processes.terminated, [117])

    async def test_restart_requeues_one_interrupted_execution_with_same_job_id(self) -> None:
        self.store.create_or_get(JobRequest("discord-1105", "repo", "change"))
        self.store.update(
            "discord-1105",
            expected=(JobState.QUEUED,),
            transform=lambda item: item.evolve(
                state=JobState.RUNNING,
                process_pid=999,
                run_attempts=1,
            ),
        )

        await self.manager.start()
        await self.wait_for(lambda: len(self.processes.launched) == 1)

        recovered = self.store.get("discord-1105")
        self.assertEqual(recovered.job_id, "discord-1105")
        self.assertEqual(recovered.state, JobState.RUNNING)
        self.assertEqual(recovered.run_attempts, 2)

    async def test_second_interruption_fails_without_a_third_execution(self) -> None:
        self.store.create_or_get(JobRequest("discord-1106", "repo", "change"))
        self.store.update(
            "discord-1106",
            expected=(JobState.QUEUED,),
            transform=lambda item: item.evolve(
                state=JobState.RUNNING,
                process_pid=999,
                run_attempts=2,
            ),
        )

        await self.manager.start()
        await self.wait_for(
            lambda: self.store.get("discord-1106").state is JobState.FAILED
        )

        self.assertEqual(self.processes.launched, [])

    async def test_interrupted_dirty_worktree_fails_without_relaunch(self) -> None:
        job_id = "discord-1120"
        worktree = self.settings.worktrees_root / job_id
        worktree.mkdir(parents=True)
        (worktree / "partial.txt").write_text("unconfirmed\n", encoding="utf-8")
        self.store.create_or_get(JobRequest(job_id, "repo", "change"))
        self.store.update(
            job_id,
            expected=(JobState.QUEUED,),
            transform=lambda item: item.evolve(
                state=JobState.RUNNING,
                process_pid=999,
                run_attempts=1,
            ),
        )

        await self.manager.start()
        await self.wait_for(lambda: self.store.get(job_id).state is JobState.FAILED)

        recovered = self.store.get(job_id)
        self.assertIn("unconfirmed", (recovered.error or "").casefold())
        self.assertEqual(self.processes.launched, [])

    async def test_publish_claims_and_launches_detached_process_without_waiting(self) -> None:
        self.store.create_or_get(JobRequest("discord-1108", "repo", "change"))
        self.store.update(
            "discord-1108",
            expected=(JobState.QUEUED,),
            transform=lambda item: item.evolve(
                state=JobState.PREPARED,
                worktree=str(self.settings.worktrees_root / "discord-1108"),
                branch="poo-ia/discord-1108-change",
                base_commit="a" * 40,
            ),
        )

        await self.manager.start()
        first, launched = await self.manager.publish("discord-1108", override=True)
        await self.wait_for(lambda: len(self.processes.launched) == 1)
        repeated, launched_again = await self.manager.publish(
            "discord-1108", override=True
        )

        self.assertTrue(launched)
        self.assertFalse(launched_again)
        self.assertEqual(first.state, JobState.PUBLISHING)
        self.assertEqual(repeated.state, JobState.PUBLISHING)
        self.assertEqual(len(self.processes.launched), 1)
        argv = self.processes.launched[0][0]
        self.assertIn("worker.job_process", argv)
        self.assertIn("--publish-only", argv)
        self.assertIn("--override", argv)
        self.assertEqual(argv[-1], "discord-1108")

    async def test_publish_is_idempotent_when_automatic_claim_wins_race(self) -> None:
        self.store.create_or_get(
            JobRequest("discord-1119", "repo", "change and publish", publish=True)
        )
        self.store.update(
            "discord-1119",
            expected=(JobState.QUEUED,),
            transform=lambda item: item.evolve(
                state=JobState.PREPARED, process_pid=119
            ),
        )
        update = self.store.update
        raced = False

        def automatic_claim(job_id, *, expected=None, transform):
            nonlocal raced
            if not raced and expected == (JobState.PREPARED,):
                raced = True
                update(
                    job_id,
                    expected=(JobState.PREPARED,),
                    transform=lambda item: item.evolve(
                        state=JobState.PUBLISHING,
                        process_pid=119,
                    ),
                )
                raise ManifestStateError("automatic publisher won")
            return update(job_id, expected=expected, transform=transform)

        self.store.update = automatic_claim

        result, created = await self.manager.publish("discord-1119")

        self.assertFalse(created)
        self.assertEqual(result.state, JobState.PUBLISHING)

    async def test_queued_publication_waits_for_existing_heavy_process(self) -> None:
        await self.manager.start()
        await self.manager.create(JobRequest("discord-1110", "repo", "first"))
        await self.wait_for(lambda: len(self.processes.launched) == 1)
        first_pid = self.processes.launched[0][1]

        self.store.create_or_get(JobRequest("discord-1111", "repo", "second"))
        self.store.update(
            "discord-1111",
            expected=(JobState.QUEUED,),
            transform=lambda item: item.evolve(state=JobState.PREPARED),
        )
        await self.manager.publish("discord-1111")
        await asyncio.sleep(0.04)
        self.assertEqual(len(self.processes.launched), 1)

        self.store.update(
            "discord-1110",
            expected=(JobState.RUNNING,),
            transform=lambda item: item.evolve(
                state=JobState.PREPARED, process_pid=None
            ),
        )
        self.processes.alive.discard(first_pid)
        await self.wait_for(lambda: len(self.processes.launched) == 2)
        self.assertIn("--publish-only", self.processes.launched[1][0])

    async def test_restart_relaunches_interrupted_explicit_publication(self) -> None:
        self.store.create_or_get(JobRequest("discord-1109", "repo", "change"))
        self.store.update(
            "discord-1109",
            expected=(JobState.QUEUED,),
            transform=lambda item: item.evolve(
                state=JobState.PUBLISHING,
                process_pid=999,
            ),
        )
        publication = self.settings.jobs_root / "discord-1109" / "publication.json"
        publication.parent.mkdir(parents=True)
        publication.write_text('{"override":true}', encoding="utf-8")
        observed_claims: list[int | None] = []
        launch = self.processes.launch

        def observe_launch(argv, *, cwd, log_path):
            observed_claims.append(self.store.get("discord-1109").process_pid)
            return launch(argv, cwd=cwd, log_path=log_path)

        self.processes.launch = observe_launch

        await self.manager.start()
        await self.wait_for(lambda: len(self.processes.launched) == 1)

        recovered = self.store.get("discord-1109")
        self.assertEqual(recovered.state, JobState.PUBLISHING)
        argv = self.processes.launched[0][0]
        self.assertIn("worker.job_process", argv)
        self.assertIn("--publish-only", argv)
        self.assertIn("--override", argv)
        self.assertEqual(observed_claims, [None])

    async def test_restart_closes_gap_between_persisting_and_claiming_publication(self) -> None:
        self.store.create_or_get(JobRequest("discord-1112", "repo", "change"))
        self.store.update(
            "discord-1112",
            expected=(JobState.QUEUED,),
            transform=lambda item: item.evolve(state=JobState.PREPARED),
        )
        save_publication_request(self.store.root, "discord-1112", override=True)

        await self.manager.start()
        await self.wait_for(lambda: len(self.processes.launched) == 1)

        self.assertEqual(self.store.get("discord-1112").state, JobState.PUBLISHING)
        self.assertIn("--override", self.processes.launched[0][0])

    async def test_finished_publication_sidecar_does_not_retry_prepared_failure(self) -> None:
        self.store.create_or_get(JobRequest("discord-1113", "repo", "change"))
        self.store.update(
            "discord-1113",
            expected=(JobState.QUEUED,),
            transform=lambda item: item.evolve(state=JobState.PREPARED),
        )
        save_publication_request(self.store.root, "discord-1113", override=True)
        finish_publication_request(self.store.root, "discord-1113")

        await self.manager.start()
        await asyncio.sleep(0.04)

        self.assertEqual(self.processes.launched, [])

    async def test_auto_publish_prepared_handoff_keeps_global_slot_and_recovers(self) -> None:
        self.store.create_or_get(
            JobRequest("discord-1114", "repo", "change and publish", publish=True)
        )
        self.store.update(
            "discord-1114",
            expected=(JobState.QUEUED,),
            transform=lambda item: item.evolve(
                state=JobState.PREPARED, process_pid=114
            ),
        )
        self.processes.alive.add(114)
        self.processes.owners[114] = "discord-1114"
        self.store.create_or_get(JobRequest("discord-1115", "repo", "later"))

        await self.manager.start()
        await asyncio.sleep(0.04)
        self.assertEqual(self.processes.launched, [])

        self.processes.alive.discard(114)
        await self.wait_for(lambda: len(self.processes.launched) == 1)
        self.assertIn("--publish-only", self.processes.launched[0][0])
        self.assertEqual(self.processes.launched[0][2], "discord-1114")

    async def test_auto_publish_handoff_is_reported_as_publishing_to_pollers(self) -> None:
        self.store.create_or_get(
            JobRequest("discord-1118", "repo", "change and publish", publish=True)
        )
        self.store.update(
            "discord-1118",
            expected=(JobState.QUEUED,),
            transform=lambda item: item.evolve(
                state=JobState.PREPARED, process_pid=118
            ),
        )

        public = await self.manager.get("discord-1118")

        self.assertEqual(public.state, JobState.PUBLISHING)
        self.assertEqual(self.store.get("discord-1118").state, JobState.PREPARED)

    async def test_cancel_active_auto_publish_handoff_cancels_codex_job(self) -> None:
        self.store.create_or_get(
            JobRequest("discord-1116", "repo", "change and publish", publish=True)
        )
        self.store.update(
            "discord-1116",
            expected=(JobState.QUEUED,),
            transform=lambda item: item.evolve(
                state=JobState.PREPARED, process_pid=116
            ),
        )
        self.processes.alive.add(116)
        self.processes.owners[116] = "discord-1116"

        cancelled = await self.manager.cancel("discord-1116")

        self.assertEqual(cancelled.state, JobState.CANCELLED)
        self.assertEqual(self.processes.terminated, [116])

    async def test_retention_runs_only_after_active_heavy_work_finishes(self) -> None:
        await self.manager.close()
        retention = FakeRetention()
        self.manager = JobManager(
            self.settings,
            store=self.store,
            processes=self.processes,
            retention=retention,
            project_root=Path(self.temporary.name),
        )
        self.store.create_or_get(JobRequest("retention-active", "repo", "change"))
        self.store.update(
            "retention-active",
            expected=(JobState.QUEUED,),
            transform=lambda item: item.evolve(
                state=JobState.RUNNING,
                process_pid=501,
            ),
        )
        self.processes.alive.add(501)
        self.processes.owners[501] = "retention-active"

        await self.manager.start()
        await asyncio.sleep(0.04)
        self.assertEqual(retention.calls, 0)

        self.store.update(
            "retention-active",
            expected=(JobState.RUNNING,),
            transform=lambda item: item.evolve(
                state=JobState.SUCCEEDED,
                process_pid=None,
            ),
        )
        self.processes.alive.discard(501)
        await self.wait_for(lambda: retention.calls == 1)

    async def test_retention_does_not_run_during_auto_publish_handoff(self) -> None:
        await self.manager.close()
        retention = FakeRetention()
        self.manager = JobManager(
            self.settings,
            store=self.store,
            processes=self.processes,
            retention=retention,
            project_root=Path(self.temporary.name),
        )
        self.store.create_or_get(
            JobRequest("retention-handoff", "repo", "change", publish=True)
        )
        self.store.update(
            "retention-handoff",
            expected=(JobState.QUEUED,),
            transform=lambda item: item.evolve(
                state=JobState.PREPARED,
                process_pid=502,
            ),
        )
        self.processes.alive.add(502)
        self.processes.owners[502] = "retention-handoff"

        await self.manager.start()
        await asyncio.sleep(0.04)

        self.assertEqual(retention.calls, 0)

    async def test_retention_holds_dispatcher_slot_before_launching_queued_job(self) -> None:
        await self.manager.close()
        entered = threading.Event()
        release = threading.Event()
        retention = FakeRetention(entered=entered, release=release)
        self.manager = JobManager(
            self.settings,
            store=self.store,
            processes=self.processes,
            retention=retention,
            project_root=Path(self.temporary.name),
        )
        await self.manager.create(JobRequest("retention-queued", "repo", "change"))

        await self.manager.start()
        acquired = await asyncio.to_thread(entered.wait, 1)
        self.assertTrue(acquired)
        self.assertEqual(self.processes.launched, [])

        release.set()
        await self.wait_for(lambda: len(self.processes.launched) == 1)
        self.assertEqual(self.processes.launched[0][2], "retention-queued")

    async def test_retention_failure_uses_interval_backoff_without_busy_spin(self) -> None:
        await self.manager.close()
        retention = FakeRetention(error=RuntimeError("temporary cleanup failure"))
        settings = replace(self.settings, retention_sweep_seconds=0.05)
        self.manager = JobManager(
            settings,
            store=self.store,
            processes=self.processes,
            retention=retention,
            project_root=Path(self.temporary.name),
        )

        await self.manager.start()
        await self.wait_for(lambda: retention.calls == 1)
        await asyncio.sleep(0.03)
        self.assertEqual(retention.calls, 1)
        await self.wait_for(lambda: retention.calls >= 2)
        self.assertLessEqual(retention.calls, 2)

    async def test_default_retention_removes_expired_terminal_manifest(self) -> None:
        self.store.create_or_get(JobRequest("retention-old", "repo", "done"))
        self.store.update(
            "retention-old",
            expected=(JobState.QUEUED,),
            transform=lambda item: item.evolve(
                state=JobState.CANCELLED,
                updated_at=datetime(2020, 1, 1, tzinfo=UTC).isoformat(),
            ),
        )

        await self.manager.start()
        path = self.settings.jobs_root / "retention-old.json"
        await self.wait_for(lambda: not path.exists())

        self.assertEqual(self.manager.status()["retention"]["last_deleted"], 1)

    async def test_corrupt_manifest_does_not_block_dispatch_or_health(self) -> None:
        corrupt = self.settings.jobs_root / "corrupt.json"
        corrupt.write_text("{broken", encoding="utf-8")
        await self.manager.create(JobRequest("after-corrupt", "repo", "change"))

        await self.manager.start()
        await self.wait_for(lambda: len(self.processes.launched) == 1)

        self.assertEqual(self.processes.launched[0][2], "after-corrupt")
        self.assertEqual(self.manager.status()["manifest_errors"], 1)


if __name__ == "__main__":
    unittest.main()
