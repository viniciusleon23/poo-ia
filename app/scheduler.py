"""Persistent single-concurrency scheduler for every heavy Poo-IA backend."""

from __future__ import annotations

import asyncio
import inspect
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .models import Job, JobStatus, ValidationStatus
from .storage import InvalidJobTransition, SQLiteStorage


JobExecutor = Callable[[Job], Awaitable["JobOutcome"]]
JobCallback = Callable[[Job], Awaitable[None] | None]
JobCanceller = Callable[[Job], Awaitable[None] | None]

DEFAULT_NOTIFICATION_RETRY_BASE_SECONDS = 1.0
DEFAULT_NOTIFICATION_RETRY_MAX_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class JobOutcome:
    """Safe final/checkpoint information returned by a heavy executor."""

    state: JobStatus
    summary: str | None = None
    safe_error: str | None = None
    branch: str | None = None
    external_reference: str | None = None
    checkpoint: Mapping[str, Any] = field(default_factory=dict)
    validation_status: ValidationStatus | None = None

    def __post_init__(self) -> None:
        if self.state not in {
            JobStatus.PREPARED,
            JobStatus.SUCCEEDED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }:
            raise ValueError("a scheduler outcome must be prepared or terminal")


async def _call_optional(callback: JobCallback | JobCanceller | None, job: Job) -> None:
    if callback is None:
        return
    result = callback(job)
    if inspect.isawaitable(result):
        await result


def _safe_error(error: BaseException) -> str:
    text = " ".join(str(error).split())[:500]
    return text or error.__class__.__name__


class PersistentScheduler:
    """Recover and execute all heavy jobs through one global dispatcher.

    SQLite is the queue of record. The asyncio task only wakes the dispatcher;
    therefore a process restart cannot lose a queued request.
    """

    def __init__(
        self,
        storage: SQLiteStorage,
        executor: JobExecutor,
        *,
        canceller: JobCanceller | None = None,
        on_finished: JobCallback | None = None,
        notification_retry_base_seconds: float = DEFAULT_NOTIFICATION_RETRY_BASE_SECONDS,
        notification_retry_max_seconds: float = DEFAULT_NOTIFICATION_RETRY_MAX_SECONDS,
    ) -> None:
        if notification_retry_base_seconds <= 0:
            raise ValueError("notification_retry_base_seconds must be positive")
        if notification_retry_max_seconds < notification_retry_base_seconds:
            raise ValueError(
                "notification_retry_max_seconds must be at least the base delay"
            )
        self.storage = storage
        self._executor = executor
        self._canceller = canceller
        self._on_finished = on_finished
        self._notification_retry_base_seconds = float(
            notification_retry_base_seconds
        )
        self._notification_retry_max_seconds = float(
            notification_retry_max_seconds
        )
        self._wake = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._dispatcher: asyncio.Task[None] | None = None
        self._active_execution: asyncio.Task[JobOutcome] | None = None
        self._active_job_id: str | None = None
        self._pending_notifications: deque[str] = deque()
        self._pending_notification_ids: set[str] = set()
        self._active_notification_job_id: str | None = None
        self._notification_lock = asyncio.Lock()
        self._notification_retry_attempts: dict[str, int] = {}
        self._notification_retry_tasks: dict[str, asyncio.Task[None]] = {}
        self._closing = False

    @property
    def active_job_id(self) -> str | None:
        return self._active_job_id

    @property
    def is_running(self) -> bool:
        return self._dispatcher is not None and not self._dispatcher.done()

    async def start(self) -> None:
        if self.is_running:
            return
        self._closing = False
        self._queue_recoverable_notifications()
        # Durable jobs may already exist before the dispatcher task is created.
        # Mark the scheduler busy first so an immediate wait_idle() cannot spin
        # on the initially-set event and starve the new dispatcher on Linux.
        if self._has_pending_work():
            self._idle.clear()
        self._dispatcher = asyncio.create_task(
            self._dispatch_loop(), name="poo-ia-heavy-scheduler"
        )
        self._wake.set()

    async def close(self) -> None:
        """Stop observation without converting recoverable work to cancelled."""
        self._closing = True
        self._wake.set()
        dispatcher = self._dispatcher
        if dispatcher is not None:
            dispatcher.cancel()
            try:
                await dispatcher
            except asyncio.CancelledError:
                pass
        self._dispatcher = None
        self._active_execution = None
        self._active_job_id = None
        retry_tasks = tuple(self._notification_retry_tasks.values())
        for retry_task in retry_tasks:
            retry_task.cancel()
        if retry_tasks:
            await asyncio.gather(*retry_tasks, return_exceptions=True)
        self._notification_retry_tasks.clear()
        self._notification_retry_attempts.clear()
        self._pending_notifications.clear()
        self._pending_notification_ids.clear()
        self._active_notification_job_id = None
        self._idle.set()

    def enqueue(self, job_id: str) -> Job:
        """Wake the durable queue for a newly registered (or recovered) job."""
        job = self.storage.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        if job.state not in {
            JobStatus.QUEUED,
            JobStatus.RUNNING,
            JobStatus.PUBLISHING,
        }:
            return job
        self._idle.clear()
        self._wake.set()
        return job

    def wake(self) -> None:
        """Wake the dispatcher after external state or queue reconciliation."""
        self._idle.clear()
        self._wake.set()

    def recover(self) -> tuple[Job, ...]:
        """Return recoverable jobs and wake their durable FIFO processing."""
        notifications_added = self._queue_recoverable_notifications()
        jobs = tuple(
            self.storage.list_jobs(
                states=(JobStatus.RUNNING, JobStatus.PUBLISHING, JobStatus.QUEUED)
            )
        )
        if jobs or notifications_added:
            self.wake()
        return jobs

    async def cancel(self, job_id: str) -> Job:
        """Cancel queued/active work and invoke its backend-specific cancellation."""
        job = self.storage.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        if job.state.is_terminal:
            return job

        # Nothing has reached an external backend while it is still queued.
        if job.state is not JobStatus.QUEUED:
            await _call_optional(self._canceller, job)

        current = self.storage.get_job(job_id)
        if current is None:
            raise KeyError(job_id)
        if not current.state.is_terminal:
            current = self.storage.transition_job(
                job_id,
                JobStatus.CANCELLED,
                message="Cancelado por el propietario.",
            )

        active = self._active_execution
        if self._active_job_id == job_id and active is not None and not active.done():
            active.cancel()
        await self._notify_finished(current)
        self._wake.set()
        return current

    async def wait_idle(self, *, timeout: float = 5.0) -> None:
        """Wait until no recoverable/runnable job remains (mainly for shutdown/tests)."""
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        while True:
            runnable = self.storage.list_jobs(
                states=(JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.PUBLISHING),
                limit=1,
            )
            if (
                not runnable
                and self._active_job_id is None
                and not self._pending_notifications
                and self._active_notification_job_id is None
                and not self._has_scheduled_notification_retries()
            ):
                return
            # An already-set asyncio.Event completes synchronously. Clear it and
            # recheck durable state before awaiting to avoid both starvation and
            # a clear-after-set race with the dispatcher.
            self._idle.clear()
            runnable = self.storage.list_jobs(
                states=(JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.PUBLISHING),
                limit=1,
            )
            if (
                not runnable
                and self._active_job_id is None
                and not self._pending_notifications
                and self._active_notification_job_id is None
                and not self._has_scheduled_notification_retries()
            ):
                return
            await asyncio.wait_for(self._idle.wait(), timeout=timeout)

    def _queue_recoverable_notifications(self) -> int:
        added = 0
        for job in self.storage.list_jobs_pending_notification():
            if (
                job.job_id == self._active_notification_job_id
                or job.job_id in self._pending_notification_ids
            ):
                continue
            self._pending_notifications.append(job.job_id)
            self._pending_notification_ids.add(job.job_id)
            added += 1
        return added

    def _next_pending_notification(self) -> Job | None:
        while self._pending_notifications:
            job_id = self._pending_notifications.popleft()
            self._pending_notification_ids.discard(job_id)
            job = self.storage.get_job(job_id)
            if job is not None and job.notification_completed_at is None:
                return job
        return None

    def _has_pending_work(self) -> bool:
        return bool(self._pending_notifications) or self._next_runnable() is not None

    def _has_scheduled_notification_retries(self) -> bool:
        return any(
            not task.done() for task in self._notification_retry_tasks.values()
        )

    def _schedule_notification_retry(self, job_id: str) -> None:
        if self._closing:
            return
        existing = self._notification_retry_tasks.get(job_id)
        if existing is not None and not existing.done():
            return
        attempt = self._notification_retry_attempts.get(job_id, 0) + 1
        self._notification_retry_attempts[job_id] = attempt
        multiplier = 2 ** min(attempt - 1, 20)
        delay = min(
            self._notification_retry_base_seconds * multiplier,
            self._notification_retry_max_seconds,
        )
        self._idle.clear()
        self._notification_retry_tasks[job_id] = asyncio.create_task(
            self._retry_notification_after(job_id, delay),
            name=f"poo-ia-notification-retry-{job_id}",
        )

    async def _retry_notification_after(self, job_id: str, delay: float) -> None:
        current_task = asyncio.current_task()
        try:
            await asyncio.sleep(delay)
            if self._closing:
                return
            if (
                job_id != self._active_notification_job_id
                and job_id not in self._pending_notification_ids
            ):
                self._pending_notifications.append(job_id)
                self._pending_notification_ids.add(job_id)
            self._idle.clear()
            self._wake.set()
        finally:
            if self._notification_retry_tasks.get(job_id) is current_task:
                self._notification_retry_tasks.pop(job_id, None)

    def _clear_notification_retry(self, job_id: str) -> None:
        self._notification_retry_attempts.pop(job_id, None)
        task = self._notification_retry_tasks.pop(job_id, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    def _next_runnable(self) -> Job | None:
        # Recover a job already claimed before considering later queued work.
        active = self.storage.list_jobs(
            states=(JobStatus.RUNNING, JobStatus.PUBLISHING), limit=1
        )
        if active:
            return active[0]
        return self.storage.next_queued_job()

    async def _dispatch_loop(self) -> None:
        while True:
            notification = self._next_pending_notification()
            if notification is not None:
                self._idle.clear()
                self._active_notification_job_id = notification.job_id
                try:
                    await self._notify_finished(notification)
                finally:
                    self._active_notification_job_id = None
                    self._wake.set()
                continue

            job = self._next_runnable()
            if job is None:
                self._idle.set()
                self._wake.clear()
                # Close the clear/wait race with a second durable check.
                if self._has_pending_work():
                    self._idle.clear()
                    continue
                await self._wake.wait()
                continue

            self._idle.clear()
            await self._execute_one(job)

    async def _execute_one(self, job: Job) -> None:
        if job.state is JobStatus.QUEUED:
            job = self.storage.transition_job(
                job.job_id, JobStatus.RUNNING, message="Ejecución iniciada."
            )
        self._active_job_id = job.job_id
        execution = asyncio.create_task(
            self._executor(job), name=f"poo-ia-job-{job.job_id}"
        )
        self._active_execution = execution
        try:
            outcome = await execution
        except asyncio.CancelledError:
            if self._closing:
                raise
            current = self.storage.get_job(job.job_id)
            if current is not None and not current.state.is_terminal:
                try:
                    current = self.storage.transition_job(
                        job.job_id,
                        JobStatus.CANCELLED,
                        message="La ejecución fue cancelada.",
                    )
                except InvalidJobTransition:
                    current = self.storage.get_job(job.job_id)
                if current is not None:
                    await self._notify_finished(current)
        except Exception as error:
            current = self.storage.get_job(job.job_id)
            if current is not None and not current.state.is_terminal:
                try:
                    failed = self.storage.transition_job(
                        job.job_id,
                        JobStatus.FAILED,
                        safe_error=_safe_error(error),
                        message="La integración terminó con un error.",
                    )
                except InvalidJobTransition:
                    failed = self.storage.get_job(job.job_id)
                if failed is not None:
                    await self._notify_finished(failed)
        else:
            current = self.storage.get_job(job.job_id)
            try:
                if current is not None and not current.state.is_terminal:
                    current = self._apply_outcome(current, outcome)
            except Exception as error:
                current = self.storage.get_job(job.job_id)
                if current is not None and not current.state.is_terminal:
                    current = self.storage.transition_job(
                        job.job_id,
                        JobStatus.FAILED,
                        safe_error=_safe_error(error),
                        message="El resultado del backend no coincidió con el estado local.",
                    )
            if current is not None:
                await self._notify_finished(current)
        finally:
            self._active_execution = None
            self._active_job_id = None
            self._wake.set()

    def _apply_outcome(self, current: Job, outcome: JobOutcome) -> Job:
        if current.state == outcome.state:
            return current

        # A remote worker can move through intermediate states between polls.
        path: list[JobStatus]
        if current.state is JobStatus.QUEUED:
            path = [JobStatus.RUNNING]
            current = self.storage.transition_job(current.job_id, JobStatus.RUNNING)
        if current.state is JobStatus.RUNNING and outcome.state is JobStatus.SUCCEEDED:
            path = [JobStatus.SUCCEEDED]
        elif current.state is JobStatus.RUNNING and outcome.state is JobStatus.PREPARED:
            path = [JobStatus.PREPARED]
        elif current.state is JobStatus.RUNNING and outcome.state in {
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }:
            path = [outcome.state]
        elif current.state is JobStatus.PREPARED and outcome.state is JobStatus.SUCCEEDED:
            path = [JobStatus.PUBLISHING, JobStatus.SUCCEEDED]
        elif current.state is JobStatus.PUBLISHING and outcome.state in {
            JobStatus.SUCCEEDED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }:
            path = [outcome.state]
        elif current.state is JobStatus.PREPARED and outcome.state in {
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }:
            path = [outcome.state]
        else:
            raise InvalidJobTransition(
                f"executor returned {outcome.state.value} for {current.state.value} job"
            )

        for index, state in enumerate(path):
            final = index == len(path) - 1
            current = self.storage.transition_job(
                current.job_id,
                state,
                message=("Resultado sincronizado." if final else "Estado remoto sincronizado."),
                branch=outcome.branch if final else current.branch,
                external_reference=(
                    outcome.external_reference if final else current.external_reference
                ),
                checkpoint=outcome.checkpoint if final else current.checkpoint,
                summary=outcome.summary if final else current.summary,
                safe_error=outcome.safe_error if final else current.safe_error,
                validation_status=(
                    outcome.validation_status if final else current.validation_status
                ),
            )
        return current

    async def _notify_finished(self, job: Job) -> None:
        if self._on_finished is None:
            return
        async with self._notification_lock:
            current = self.storage.get_job(job.job_id)
            if current is None or current.notification_completed_at is not None:
                self._clear_notification_retry(job.job_id)
                return
            try:
                await _call_optional(self._on_finished, current)
                self.storage.mark_job_notification_completed(current.job_id)
                self._clear_notification_retry(current.job_id)
            except Exception as error:
                # The empty durable ACK survives restarts. A bounded timer also
                # retries while this process remains alive without busy-spinning.
                self.storage.record_job_progress(
                    current.job_id,
                    f"No se pudo preparar la notificación: {_safe_error(error)}",
                )
                self._schedule_notification_retry(current.job_id)
