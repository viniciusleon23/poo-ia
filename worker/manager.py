"""Single-concurrency durable worker dispatcher."""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from .config import WorkerSettings
from .github import GitHubPublisher
from .models import JobManifest, JobRequest, JobState, TERMINAL_STATES
from .processes import DetachedProcessController, ProcessController
from .publication import (
    finish_publication_request,
    load_publication_override,
    publication_is_pending,
    save_publication_request,
)
from .repositories import RepositoryResolver
from .retention import RetentionReaper, RetentionReport
from .store import ManifestStateError, ManifestStore
from .validation_sandbox import DockerValidationRunner


LOGGER = logging.getLogger(__name__)


class JobManager:
    def __init__(
        self,
        settings: WorkerSettings,
        *,
        store: ManifestStore | None = None,
        processes: ProcessController | None = None,
        publisher: GitHubPublisher | None = None,
        repositories: RepositoryResolver | None = None,
        retention: RetentionReaper | None = None,
        project_root: Path | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.store = store or ManifestStore(settings.jobs_root)
        self.processes = processes or DetachedProcessController()
        self.publisher = publisher or GitHubPublisher(settings, self.store)
        self.repositories = repositories or RepositoryResolver(
            settings.workspace,
            settings.worktrees_root,
            git_executable=settings.git_executable,
        )
        self.retention = retention or RetentionReaper(
            self.store,
            workspace=settings.workspace,
            worktrees_root=settings.worktrees_root,
            retention_days=settings.operational_retention_days,
            git_executable=settings.git_executable,
        )
        self.project_root = project_root or Path(__file__).resolve().parent.parent
        self._monotonic = monotonic
        self._wake = asyncio.Event()
        self._dispatcher: asyncio.Task[None] | None = None
        self._closing = False
        self._publish_lock = asyncio.Lock()
        self._next_retention_at = 0.0
        self._last_retention_report: RetentionReport | None = None
        self._last_retention_failed = False
        self._retention_task: asyncio.Task[RetentionReport] | None = None
        self._validation_sandbox = (
            DockerValidationRunner(
                staging_root=settings.data_root / "validation",
                docker_executable=settings.docker_executable,
                build_timeout_seconds=settings.validation_build_timeout_seconds,
            )
            if settings.validation_enabled else None
        )

    async def start(self) -> None:
        if self._dispatcher is None:
            self._closing = False
            if self._validation_sandbox is not None:
                await asyncio.to_thread(self._validation_sandbox.cleanup_orphans)
            self._dispatcher = asyncio.create_task(
                self._dispatch_loop(), name="poo-ia-worker-dispatcher"
            )

    async def close(self) -> None:
        self._closing = True
        self._wake.set()
        if self._dispatcher is not None:
            self._dispatcher.cancel()
            try:
                await self._dispatcher
            except asyncio.CancelledError:
                pass
            self._dispatcher = None

    async def create(self, request: JobRequest) -> tuple[JobManifest, bool]:
        manifest, created = self.store.create_or_get(request)
        if created:
            self._wake.set()
        return manifest, created

    async def get(self, job_id: str) -> JobManifest:
        manifest = self.store.get(job_id)
        if (
            manifest.state is JobState.PREPARED
            and manifest.requested_publish
            and manifest.process_pid is not None
        ):
            # PREPARED is only the private hand-off between Codex and automatic
            # publication here. Reporting PUBLISHING prevents a poller from
            # treating a still-owned job as a final prepared result.
            return replace(manifest, state=JobState.PUBLISHING)
        return manifest

    async def repository_names(self) -> tuple[str, ...]:
        from app.repository_scope import execution_repositories
        return execution_repositories(await asyncio.to_thread(self.repositories.clean_names))

    async def cancel(self, job_id: str) -> JobManifest:
        current = self.store.get(job_id)
        if current.state in TERMINAL_STATES:
            return current
        if current.state is JobState.PREPARED:
            # Automatic publication intentionally exposes a very small durable
            # PREPARED hand-off while the original job process is still alive.
            # It is still active work, so cancellation must win before that
            # process can claim PUBLISHING or the dispatcher can recover it.
            if current.requested_publish and current.process_pid is not None:
                updated = self.store.update(
                    job_id,
                    expected=(JobState.PREPARED,),
                    transform=lambda manifest: manifest.evolve(
                        state=JobState.CANCELLED,
                        process_pid=None,
                        error=None,
                    ),
                )
                if self.processes.is_job_process(
                    current.process_pid, current.job_id
                ):
                    self.processes.terminate_group(current.process_pid)
                finish_publication_request(self.store.root, job_id)
                self._wake.set()
                return updated
            raise ManifestStateError(
                f"a {current.state.value} job cannot be cancelled safely"
            )
        if current.state is JobState.PUBLISHING:
            if (
                current.process_pid
                and self.processes.is_job_process(current.process_pid, current.job_id)
            ):
                self.processes.terminate_group(current.process_pid)
            await self._cleanup_validation(current)
            # Stop the external Git/GitHub process before releasing its durable
            # claim. If it completed first, return that actual terminal result
            # (including its PR URL) instead of overwriting it with PREPARED.
            latest = self.store.get(job_id)
            if latest.state in TERMINAL_STATES or latest.state is JobState.PREPARED:
                finish_publication_request(self.store.root, job_id)
                self._wake.set()
                return latest
            updated = self.store.update(
                job_id,
                expected=(JobState.PUBLISHING,),
                transform=lambda manifest: manifest.evolve(
                    state=JobState.PREPARED,
                    process_pid=None,
                    error="Publication was cancelled; the prepared change was preserved.",
                ),
            )
            finish_publication_request(self.store.root, job_id)
            self._wake.set()
            return updated
        updated = self.store.update(
            job_id,
            expected=(JobState.QUEUED, JobState.RUNNING),
            transform=lambda manifest: manifest.evolve(
                state=JobState.CANCELLED,
                process_pid=None,
                error=None,
            ),
        )
        if (
            current.process_pid
            and self.processes.is_job_process(current.process_pid, current.job_id)
        ):
            self.processes.terminate_group(current.process_pid)
        self._wake.set()
        await self._cleanup_validation(current)
        return updated

    async def _cleanup_validation(self, manifest: JobManifest) -> None:
        if self._validation_sandbox is None:
            return
        paths = [self.store.root / manifest.job_id / "baseline"]
        if manifest.worktree:
            path = Path(manifest.worktree).resolve()
            if path.is_relative_to(self.settings.worktrees_root.resolve()):
                paths.append(path)
        for path in paths:
            await asyncio.to_thread(self._validation_sandbox.cleanup_for_repository, path)

    async def publish(
        self, job_id: str, *, override: bool = False
    ) -> tuple[JobManifest, bool]:
        """Durably queue publication and return without waiting for Git or GitHub."""
        async with self._publish_lock:
            current = self.store.get(job_id)
            from app.repository_scope import is_documentation_repository
            if is_documentation_repository(current.repository):
                raise ManifestStateError("El brain es documental y no recibe PR de ejecución.")
            if current.pr_url or current.state is JobState.SUCCEEDED:
                return current, False
            if current.state is JobState.PUBLISHING:
                self._wake.set()
                return current, False
            if current.state is not JobState.PREPARED:
                raise ManifestStateError(
                    f"job {job_id} is {current.state.value}, not prepared"
                )
            save_publication_request(self.store.root, job_id, override=override)
            try:
                claimed = self.store.update(
                    job_id,
                    expected=(JobState.PREPARED,),
                    transform=lambda manifest: manifest.evolve(
                        state=JobState.PUBLISHING,
                        process_pid=None,
                        error=None,
                    ),
                )
            except ManifestStateError:
                # An automatic publisher can claim the PREPARED hand-off in
                # parallel with this explicit request. Treat that winner as the
                # same idempotent publication instead of surfacing a false 409.
                latest = self.store.get(job_id)
                if latest.pr_url or latest.state in {
                    JobState.PUBLISHING,
                    JobState.SUCCEEDED,
                }:
                    self._wake.set()
                    return latest, False
                raise
            self._wake.set()
            return claimed, True

    def status(self) -> dict[str, object]:
        scan = self.store.scan()
        manifests = scan.manifests
        counts = {state.value: 0 for state in JobState}
        for manifest in manifests:
            counts[manifest.state.value] += 1
        return {
            "status": "ok",
            "queue_depth": counts[JobState.QUEUED.value],
            "states": counts,
            "manifest_errors": len(scan.failures),
            "validation": {"enabled": self.settings.validation_enabled, "backend": "docker-uv"},
            "aws": {"enabled": self.settings.aws_enabled, "mode": "read-only"},
            "retention": {
                "last_deleted": (
                    len(self._last_retention_report.deleted)
                    if self._last_retention_report is not None
                    else 0
                ),
                "last_deferred": (
                    len(self._last_retention_report.deferred)
                    if self._last_retention_report is not None
                    else 0
                ),
                "last_failed": self._last_retention_failed,
            },
        }

    def _has_active_process(self, *, excluding: str | None = None) -> bool:
        for manifest in self.store.scan().manifests:
            if manifest.job_id == excluding:
                continue
            if self._is_active_manifest(manifest):
                if (
                    manifest.process_pid
                    and self.processes.is_job_process(
                        manifest.process_pid, manifest.job_id
                    )
                ):
                    return True
        return False

    @staticmethod
    def _is_active_manifest(manifest: JobManifest) -> bool:
        """Include Codex's durable PREPARED→PUBLISHING hand-off as active."""
        return manifest.state in {JobState.RUNNING, JobState.PUBLISHING} or (
            manifest.state is JobState.PREPARED
            and manifest.requested_publish
            and manifest.process_pid is not None
        )

    async def _dispatch_loop(self) -> None:
        while not self._closing:
            self._wake.clear()
            manifests = self.store.scan().manifests
            active = next(
                (
                    item
                    for item in manifests
                    if self._is_active_manifest(item)
                ),
                None,
            )
            if active is not None:
                if active.process_pid and self.processes.is_job_process(
                    active.process_pid, active.job_id
                ):
                    await self._wait_or_wake()
                    continue
                # A detached child always writes a terminal/prepared state before exit.
                # Re-read once to avoid overwriting the child's final atomic update.
                await asyncio.sleep(0)
                latest = self.store.get(active.job_id)
                if latest.state in {JobState.RUNNING, JobState.PUBLISHING}:
                    await self._cleanup_validation(latest)
                if (
                    latest.state is JobState.PREPARED
                    and latest.requested_publish
                    and latest.process_pid is not None
                ):
                    if self._has_active_process(excluding=latest.job_id):
                        await self._wait_or_wake()
                        continue
                    save_publication_request(
                        self.store.root, latest.job_id, override=False
                    )
                    try:
                        latest = self.store.update(
                            latest.job_id,
                            expected=(JobState.PREPARED,),
                            transform=lambda current: current.evolve(
                                state=JobState.PUBLISHING,
                                process_pid=None,
                                error=None,
                            ),
                        )
                    except ManifestStateError:
                        continue
                    self._launch_publication(latest, override=False)
                    continue
                if latest.state in {JobState.RUNNING, JobState.PUBLISHING}:
                    if latest.state is JobState.PUBLISHING:
                        if self._has_active_process(excluding=latest.job_id):
                            await self._wait_or_wake()
                            continue
                        override = load_publication_override(
                            self.store.root, latest.job_id
                        )
                        if override is None and not latest.requested_publish:
                            try:
                                self.store.update(
                                    latest.job_id,
                                    expected=(JobState.PUBLISHING,),
                                    transform=lambda current: current.evolve(
                                        state=JobState.PREPARED,
                                        process_pid=None,
                                        error=(
                                            "Publication ownership was lost; "
                                            "an explicit retry is required."
                                        ),
                                    ),
                                )
                            except ManifestStateError:
                                pass
                            continue
                        if override is None:
                            override = False
                            save_publication_request(
                                self.store.root, latest.job_id, override=False
                            )
                        try:
                            latest = self.store.update(
                                latest.job_id,
                                expected=(JobState.PUBLISHING,),
                                transform=lambda current: current.evolve(
                                    process_pid=None
                                ),
                            )
                        except ManifestStateError:
                            continue
                        self._launch_publication(latest, override=override)
                        continue
                    if latest.run_attempts < 2:
                        retry_is_safe = self.repositories.interrupted_retry_is_safe(
                            job_id=latest.job_id,
                            worktree=latest.worktree,
                            branch=latest.branch,
                            base_commit=latest.base_commit,
                        )
                        if not retry_is_safe:
                            try:
                                self.store.update(
                                    active.job_id,
                                    expected=(JobState.RUNNING,),
                                    transform=lambda current: current.evolve(
                                        state=JobState.FAILED,
                                        process_pid=None,
                                        error=(
                                            "Interrupted execution left unconfirmed "
                                            "worktree state; automatic rerun is blocked."
                                        ),
                                    ),
                                )
                            except ManifestStateError:
                                pass
                            continue
                        try:
                            self.store.update(
                                active.job_id,
                                expected=(JobState.RUNNING,),
                                transform=lambda current: current.evolve(
                                    state=JobState.QUEUED,
                                    process_pid=None,
                                    error="Interrupted execution is being resumed once.",
                                ),
                            )
                        except ManifestStateError:
                            pass
                        continue
                    try:
                        self.store.update(
                            active.job_id,
                            expected=(latest.state,),
                            transform=lambda current: current.evolve(
                                state=JobState.FAILED,
                                process_pid=None,
                                error="The detached job process ended before recording a result.",
                            ),
                        )
                    except ManifestStateError:
                        pass
                continue

            if self._monotonic() >= self._next_retention_at:
                await self._run_retention()
                manifests = self.store.scan().manifests

            queued = next(
                (item for item in manifests if item.state is JobState.QUEUED), None
            )
            publication = next(
                (
                    item
                    for item in manifests
                    if item.state is JobState.PREPARED
                    and publication_is_pending(self.store.root, item.job_id)
                ),
                None,
            )
            if publication is not None:
                override = load_publication_override(
                    self.store.root, publication.job_id
                )
                try:
                    publication = self.store.update(
                        publication.job_id,
                        expected=(JobState.PREPARED,),
                        transform=lambda current: current.evolve(
                            state=JobState.PUBLISHING,
                            process_pid=None,
                            error=None,
                        ),
                    )
                except ManifestStateError:
                    continue
                self._launch_publication(
                    publication, override=False if override is None else override
                )
                continue
            if queued is not None:
                self._launch(queued)
                continue
            await self._wait_or_wake()

    async def _run_retention(self) -> None:
        """Run bounded maintenance inside the same global heavy-work gate."""
        task = asyncio.create_task(
            asyncio.to_thread(self.retention.prune_once),
            name="poo-ia-worker-retention",
        )
        self._retention_task = task
        try:
            report = await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await task
            except Exception as error:
                LOGGER.warning("Worker retention sweep failed while closing: %s", error)
            raise
        except Exception as error:
            self._last_retention_failed = True
            LOGGER.warning("Worker retention sweep failed: %s", error)
            delay = min(
                self.settings.retention_sweep_seconds,
                max(60.0, self.settings.poll_interval_seconds),
            )
        else:
            self._last_retention_report = report
            self._last_retention_failed = False
            if report.deferred:
                LOGGER.warning(
                    "Worker retention deferred %d item(s)", len(report.deferred)
                )
            delay = (
                max(1.0, self.settings.poll_interval_seconds)
                if report.has_more
                else self.settings.retention_sweep_seconds
            )
        finally:
            self._retention_task = None
        self._next_retention_at = self._monotonic() + delay

    def _launch_publication(self, manifest: JobManifest, *, override: bool) -> None:
        """Launch a claimed publication in the same owned process mechanism as Codex."""
        job_directory = self.store.root / manifest.job_id
        log_path = job_directory / "publication.log"
        arguments = [
            sys.executable,
            "-m",
            "worker.job_process",
            "--publish-only",
        ]
        if override:
            arguments.append("--override")
        arguments.append(manifest.job_id)
        try:
            pid = self.processes.launch(
                tuple(arguments), cwd=self.project_root, log_path=log_path
            )
            try:
                self.store.update(
                    manifest.job_id,
                    expected=(JobState.PUBLISHING,),
                    transform=lambda current: current.evolve(process_pid=pid),
                )
            except ManifestStateError:
                # The detached process can finish before its PID is recorded.
                pass
        except OSError as error:
            safe = " ".join(str(error).split())[:500]
            try:
                self.store.update(
                    manifest.job_id,
                    expected=(JobState.PUBLISHING,),
                    transform=lambda current: current.evolve(
                        state=JobState.PREPARED,
                        process_pid=None,
                        error=safe or "Could not launch the publication process.",
                    ),
                )
            except ManifestStateError:
                pass
            finish_publication_request(self.store.root, manifest.job_id)

    def _launch(self, manifest: JobManifest) -> None:
        job_directory = self.store.root / manifest.job_id
        log_path = job_directory / "worker.log"
        try:
            self.store.update(
                manifest.job_id,
                expected=(JobState.QUEUED,),
                transform=lambda current: current.evolve(
                    state=JobState.RUNNING,
                    process_pid=None,
                    run_attempts=current.run_attempts + 1,
                    error=None,
                ),
            )
            pid = self.processes.launch(
                (sys.executable, "-m", "worker.job_process", manifest.job_id),
                cwd=self.project_root,
                log_path=log_path,
            )
            try:
                self.store.update(
                    manifest.job_id,
                    expected=(JobState.RUNNING,),
                    transform=lambda current: current.evolve(process_pid=pid),
                )
            except ManifestStateError:
                # The detached process may already have completed.
                pass
        except ManifestStateError:
            return
        except OSError as error:
            safe = " ".join(str(error).split())[:500]
            try:
                self.store.update(
                    manifest.job_id,
                    expected=(JobState.RUNNING,),
                    transform=lambda current: current.evolve(
                        state=JobState.FAILED,
                        process_pid=None,
                        error=safe or "Could not launch the detached worker process.",
                    ),
                )
            except ManifestStateError:
                pass

    async def _wait_or_wake(self) -> None:
        try:
            await asyncio.wait_for(
                self._wake.wait(), timeout=self.settings.poll_interval_seconds
            )
        except TimeoutError:
            pass
