from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from app.models import InboundMessage, JobKind, JobStatus
from app.outbox import DurableOutbox
from app.scheduler import JobOutcome, PersistentScheduler
from app.storage import SQLiteStorage


def queued_job(storage: SQLiteStorage, message_id: int, kind: JobKind = JobKind.RESEARCH):
    return storage.register_inbound(
        InboundMessage(message_id, 100, 200, f"work-{message_id}"),
        job_kind=kind,
    ).job


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.storage = SQLiteStorage(":memory:")

    async def asyncTearDown(self) -> None:
        self.storage.close()

    async def test_all_heavy_kinds_share_one_fifo_execution_slot(self) -> None:
        jobs = [
            queued_job(self.storage, 1, JobKind.RESEARCH),
            queued_job(self.storage, 2, JobKind.CODEX),
            queued_job(self.storage, 3, JobKind.PUBLISH),
        ]
        active = 0
        maximum_active = 0
        order: list[str] = []

        async def executor(job):
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            order.append(job.job_id)
            await asyncio.sleep(0.005)
            active -= 1
            return JobOutcome(JobStatus.SUCCEEDED, summary="done")

        scheduler = PersistentScheduler(self.storage, executor)
        await scheduler.start()
        for job in jobs:
            scheduler.enqueue(job.job_id)
        await scheduler.wait_idle(timeout=2)
        await scheduler.close()

        self.assertEqual(maximum_active, 1)
        self.assertEqual(order, [job.job_id for job in jobs])
        self.assertTrue(
            all(self.storage.get_job(job.job_id).state is JobStatus.SUCCEEDED for job in jobs)
        )

    async def test_start_recovers_a_previously_running_job_once(self) -> None:
        job = queued_job(self.storage, 4)
        self.storage.transition_job(job.job_id, JobStatus.RUNNING)
        calls: list[str] = []

        async def executor(recovered):
            calls.append(recovered.job_id)
            return JobOutcome(JobStatus.SUCCEEDED)

        scheduler = PersistentScheduler(self.storage, executor)
        self.assertEqual([item.job_id for item in scheduler.recover()], [job.job_id])
        await scheduler.start()
        await scheduler.wait_idle(timeout=2)
        await scheduler.close()

        self.assertEqual(calls, [job.job_id])
        self.assertEqual(self.storage.get_job(job.job_id).state, JobStatus.SUCCEEDED)

    async def test_executor_error_fails_job_without_stopping_next_job(self) -> None:
        first = queued_job(self.storage, 5)
        second = queued_job(self.storage, 6)

        async def executor(job):
            if job.job_id == first.job_id:
                raise RuntimeError("safe simulated error")
            return JobOutcome(JobStatus.SUCCEEDED)

        scheduler = PersistentScheduler(self.storage, executor)
        await scheduler.start()
        await scheduler.wait_idle(timeout=2)
        self.assertTrue(scheduler.is_running)
        await scheduler.close()

        failed = self.storage.get_job(first.job_id)
        self.assertEqual(failed.state, JobStatus.FAILED)
        self.assertEqual(failed.safe_error, "safe simulated error")
        self.assertEqual(self.storage.get_job(second.job_id).state, JobStatus.SUCCEEDED)

    async def test_cancel_active_job_calls_backend_and_keeps_dispatcher_alive(self) -> None:
        first = queued_job(self.storage, 7)
        second = queued_job(self.storage, 8)
        entered = asyncio.Event()
        cancelled_externally: list[str] = []

        async def executor(job):
            if job.job_id == first.job_id:
                entered.set()
                await asyncio.Event().wait()
            return JobOutcome(JobStatus.SUCCEEDED)

        async def canceller(job):
            cancelled_externally.append(job.job_id)

        scheduler = PersistentScheduler(self.storage, executor, canceller=canceller)
        await scheduler.start()
        await asyncio.wait_for(entered.wait(), timeout=1)
        cancelled = await scheduler.cancel(first.job_id)
        await scheduler.wait_idle(timeout=2)
        await scheduler.close()

        self.assertEqual(cancelled.state, JobStatus.CANCELLED)
        self.assertEqual(cancelled_externally, [first.job_id])
        self.assertEqual(self.storage.get_job(second.job_id).state, JobStatus.SUCCEEDED)

    async def test_close_leaves_active_job_recoverable_instead_of_cancelling_it(self) -> None:
        job = queued_job(self.storage, 9)
        entered = asyncio.Event()

        async def executor(_job):
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        scheduler = PersistentScheduler(self.storage, executor)
        await scheduler.start()
        await asyncio.wait_for(entered.wait(), timeout=1)
        await scheduler.close()

        self.assertEqual(self.storage.get_job(job.job_id).state, JobStatus.RUNNING)

    async def test_queued_cancel_never_calls_external_backend(self) -> None:
        job = queued_job(self.storage, 10)
        calls: list[str] = []

        async def executor(_job):
            return JobOutcome(JobStatus.SUCCEEDED)

        async def canceller(item):
            calls.append(item.job_id)

        scheduler = PersistentScheduler(self.storage, executor, canceller=canceller)
        cancelled = await scheduler.cancel(job.job_id)

        self.assertEqual(cancelled.state, JobStatus.CANCELLED)
        self.assertEqual(calls, [])

    async def test_start_recovers_notifications_for_every_final_outcome(self) -> None:
        prepared = queued_job(self.storage, 11, JobKind.CODEX)
        succeeded = queued_job(self.storage, 12)
        failed = queued_job(self.storage, 13)
        cancelled = queued_job(self.storage, 14)
        for job in (prepared, succeeded, failed):
            self.storage.transition_job(job.job_id, JobStatus.RUNNING)
        self.storage.transition_job(prepared.job_id, JobStatus.PREPARED)
        self.storage.transition_job(succeeded.job_id, JobStatus.SUCCEEDED)
        self.storage.transition_job(failed.job_id, JobStatus.FAILED)
        self.storage.transition_job(cancelled.job_id, JobStatus.CANCELLED)
        callbacks: list[str] = []

        async def executor(_job):
            raise AssertionError("final jobs must not execute again")

        async def on_finished(job):
            callbacks.append(job.job_id)

        scheduler = PersistentScheduler(
            self.storage, executor, on_finished=on_finished
        )
        await scheduler.start()
        await scheduler.wait_idle(timeout=2)
        await scheduler.close()

        expected = [prepared.job_id, succeeded.job_id, failed.job_id, cancelled.job_id]
        self.assertEqual(callbacks, expected)
        self.assertEqual(self.storage.list_jobs_pending_notification(), [])
        self.assertTrue(
            all(
                self.storage.get_job(job_id).notification_completed_at is not None
                for job_id in expected
            )
        )

    async def test_failed_callback_recovers_after_restart_with_one_durable_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "scheduler.sqlite3"
            attempts = 0
            outbox_ids: list[str] = []
            first_attempt = asyncio.Event()

            def callback_for(storage):
                async def on_finished(job):
                    nonlocal attempts
                    attempts += 1
                    envelope = DurableOutbox(storage).enqueue(
                        job.request_id, f"resultado {job.job_id}"
                    )
                    outbox_ids.append(envelope.outbox_id)
                    if attempts == 1:
                        first_attempt.set()
                        raise RuntimeError("simulated crash after durable enqueue")

                return on_finished

            first_storage = SQLiteStorage(path)
            job = queued_job(first_storage, 15)

            async def executor(_job):
                return JobOutcome(JobStatus.SUCCEEDED, summary="done")

            first = PersistentScheduler(
                first_storage,
                executor,
                on_finished=callback_for(first_storage),
                notification_retry_base_seconds=60,
                notification_retry_max_seconds=60,
            )
            await first.start()
            await asyncio.wait_for(first_attempt.wait(), timeout=2)
            await asyncio.sleep(0)
            await first.close()
            self.assertEqual(attempts, 1)
            self.assertIsNone(
                first_storage.get_job(job.job_id).notification_completed_at
            )
            first_storage.close()

            second_storage = SQLiteStorage(path)

            async def must_not_execute(_job):
                raise AssertionError("a finished job must only recover its callback")

            second = PersistentScheduler(
                second_storage,
                must_not_execute,
                on_finished=callback_for(second_storage),
            )
            await second.start()
            await second.wait_idle(timeout=2)
            self.assertEqual(attempts, 2)
            self.assertEqual(len(set(outbox_ids)), 1)
            pending = DurableOutbox(second_storage).pending()
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0].outbox_id, outbox_ids[0])
            self.assertIsNotNone(
                second_storage.get_job(job.job_id).notification_completed_at
            )

            second.recover()
            await second.wait_idle(timeout=2)
            await asyncio.sleep(0)
            self.assertEqual(attempts, 2)
            await second.close()
            second_storage.close()

    async def test_failed_callback_retries_live_with_backoff(self) -> None:
        job = queued_job(self.storage, 16)
        self.storage.transition_job(job.job_id, JobStatus.RUNNING)
        self.storage.transition_job(job.job_id, JobStatus.SUCCEEDED)
        attempts = 0
        first_attempt = asyncio.Event()

        async def executor(_job):
            raise AssertionError("a finished job must not execute again")

        async def on_finished(_job):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                first_attempt.set()
                raise RuntimeError("temporary callback failure")
            DurableOutbox(self.storage).enqueue(
                job.request_id, f"resultado {job.job_id}"
            )

        scheduler = PersistentScheduler(
            self.storage,
            executor,
            on_finished=on_finished,
            notification_retry_base_seconds=0.05,
            notification_retry_max_seconds=0.1,
        )
        await scheduler.start()
        await asyncio.wait_for(first_attempt.wait(), timeout=1)
        await asyncio.sleep(0.01)
        self.assertEqual(attempts, 1)
        await scheduler.wait_idle(timeout=2)
        self.assertEqual(attempts, 2)
        self.assertIsNotNone(
            self.storage.get_job(job.job_id).notification_completed_at
        )
        self.assertEqual(len(DurableOutbox(self.storage).pending()), 1)
        await scheduler.close()

    async def test_close_cancels_pending_notification_retry(self) -> None:
        job = queued_job(self.storage, 17)
        self.storage.transition_job(job.job_id, JobStatus.RUNNING)
        self.storage.transition_job(job.job_id, JobStatus.SUCCEEDED)
        attempted = asyncio.Event()
        attempts = 0

        async def executor(_job):
            raise AssertionError("a finished job must not execute again")

        async def on_finished(_job):
            nonlocal attempts
            attempts += 1
            attempted.set()
            raise RuntimeError("persistent callback failure")

        scheduler = PersistentScheduler(
            self.storage,
            executor,
            on_finished=on_finished,
            notification_retry_base_seconds=0.2,
            notification_retry_max_seconds=0.2,
        )
        await scheduler.start()
        await asyncio.wait_for(attempted.wait(), timeout=1)
        await asyncio.sleep(0)
        await scheduler.close()
        await asyncio.sleep(0.25)

        self.assertEqual(attempts, 1)
        self.assertIsNone(
            self.storage.get_job(job.job_id).notification_completed_at
        )
