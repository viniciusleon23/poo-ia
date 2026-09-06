"""Transport-neutral orchestration for Discord today and the web UI later."""

from __future__ import annotations

import asyncio
import re
import time
import unicodedata
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .memory import MemoryStore
from .models import (
    Backend,
    ConversationKey,
    InboundMessage,
    InboundRequest,
    Intent,
    Job,
    JobKind,
    JobStatus,
    OutboxPart,
    RequestStatus,
    RouteDecision,
    ValidationStatus,
)
from .outbox import DurableOutbox
from .prompt_loader import build_prompt, load_prompt_context
from .preflight import build_preflight_request, parse_preflight
from .repository_scope import (
    DOCUMENTATION_REPOSITORY_READ_ONLY,
    UNKNOWN_REPOSITORY,
    is_documentation_repository,
    mentioned_execution_repositories,
)
from .router import (
    AMBIGUOUS_REPOSITORY,
    REPOSITORY_REQUIRED,
    explicitly_requests_code_change,
    route_message,
)
from .scheduler import JobOutcome, PersistentScheduler
from .storage import InvalidJobTransition, SQLiteStorage
from .worker_client import WorkerNotFoundError


AWS_DISABLED_MESSAGE = (
    "La integración con AWS y DynamoDB está pospuesta en esta fase. "
    "El bot principal funciona sin credenciales AWS."
)
REPOSITORY_REQUIRED_MESSAGE = (
    "Necesito que indiques un único repositorio para preparar ese cambio."
)
AMBIGUOUS_REPOSITORY_MESSAGE = (
    "Encontré más de un repositorio posible. Dime exactamente cuál debo usar."
)
JOB_NOT_FOUND_MESSAGE = "No encontré un trabajo que corresponda a esa referencia."
WORKER_DISABLED_MESSAGE = "El worker de cambios todavía no está disponible."
OPENCODE_DISABLED_MESSAGE = "El cerebro documental todavía no está disponible."
GENERATION_FAILED_MESSAGE = "No pude generar la respuesta en este momento."
PROCESSING_FAILED_MESSAGE = "No pude procesar ese mensaje. Inténtalo de nuevo en un momento."
_OPENCODE_CHECKPOINT_KEYS = ("opencode_session_id", "preflight_session_id")
OPENCODE_RECOVERY_CLEANUP_TIMEOUT_SECONDS = 5.0
OPENCODE_RECOVERY_FAILED_MESSAGE = (
    "No pude confirmar la limpieza de una sesión documental anterior; "
    "el trabajo se cerró sin iniciar un reemplazo."
)


class OllamaLike(Protocol):
    async def generate(self, prompt: str) -> str: ...


class OpenCodeLike(Protocol):
    active_session_id: str | None

    async def research(self, prompt: str, **kwargs: object) -> str: ...

    async def abort_active(self) -> str | None: ...

    async def cleanup_session(self, session_id: str) -> None: ...


class WorkerLike(Protocol):
    async def create_codex_job(self, **kwargs: object) -> Mapping[str, object]: ...

    async def get_job(self, job_id: str) -> Mapping[str, object]: ...

    async def cancel_job(self, job_id: str) -> Mapping[str, object]: ...

    async def publish_job(
        self, job_id: str, *, override: bool = False
    ) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class Submission:
    """Immediate result of accepting a transport-neutral inbound message."""

    request_id: str
    created: bool
    intent: Intent | None
    job_id: str | None = None
    queued: bool = False


def _safe_text(error: BaseException) -> str:
    return " ".join(str(error).split())[:500] or error.__class__.__name__


def _normalized_words(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    plain = "".join(
        character
        for character in decomposed
        if unicodedata.category(character) != "Mn"
    )
    return re.sub(r"[^a-z0-9]+", " ", plain).strip()


class PooIAOrchestrator:
    """Own routing, context, durable work, and outbound delivery preparation."""

    def __init__(
        self,
        *,
        storage: SQLiteStorage,
        memory: MemoryStore,
        outbox: DurableOutbox,
        ollama: OllamaLike,
        content_root: Path,
        opencode: OpenCodeLike | None = None,
        worker: WorkerLike | None = None,
        repositories: Iterable[str] = (),
        worker_poll_seconds: float = 2.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if worker_poll_seconds <= 0:
            raise ValueError("worker_poll_seconds must be positive")
        self.storage = storage
        self.memory = memory
        self.outbox = outbox
        self.ollama = ollama
        self.opencode = opencode
        self.worker = worker
        self.repositories = tuple(
            dict.fromkeys(repository.strip() for repository in repositories if repository.strip())
        )
        self.instructions = load_prompt_context(content_root)
        self.worker_poll_seconds = worker_poll_seconds
        self._clock = clock
        self._output_available = asyncio.Event()
        self._start_lock = asyncio.Lock()
        self._direct_recovery_complete = False
        self._initial_session_sweep_complete = False
        self._terminal_session_cleanup: asyncio.Task[None] | None = None
        self._terminal_session_cleanup_requested = False
        self._remote_session_cleanups: dict[
            tuple[str, str, str], asyncio.Task[bool]
        ] = {}
        self.scheduler = PersistentScheduler(
            storage,
            self._execute_heavy_job,
            canceller=self._cancel_external_job,
            on_finished=self._on_job_finished,
        )

    async def start(self) -> None:
        """Recover durable jobs and make existing pending output observable."""
        async with self._start_lock:
            if not self._initial_session_sweep_complete:
                # Before the scheduler starts, a non-terminal persisted remote
                # session is orphaned by definition. Gate recovery briefly so
                # no replacement can race it, but never inherit the 300-second
                # research timeout during Discord startup.
                await self._cleanup_orphaned_opencode_sessions(terminal=False)
                self._initial_session_sweep_complete = True
            self._schedule_terminal_session_cleanup()
            if not self._direct_recovery_complete:
                for message in self.storage.list_recoverable_direct_messages():
                    await self._recover_direct_message(message)
                self._direct_recovery_complete = True
            await self.scheduler.start()
        # Let an immediate cleanup complete without waiting for slow or broken
        # OpenCode I/O. The task remains tracked and retryable on later starts.
        await asyncio.sleep(0)
        if self.outbox.pending():
            self._output_available.set()

    async def recover(self) -> None:
        """Explicit startup alias used by adapters after reconnect/restart."""
        await self.start()
        self.scheduler.recover()

    async def close(self) -> None:
        cleanup = self._terminal_session_cleanup
        if cleanup is not None and not cleanup.done():
            cleanup.cancel()
            await asyncio.gather(cleanup, return_exceptions=True)
        self._terminal_session_cleanup = None
        self._terminal_session_cleanup_requested = False
        remote_cleanups = tuple(self._remote_session_cleanups.values())
        for remote_cleanup in remote_cleanups:
            remote_cleanup.cancel()
        if remote_cleanups:
            await asyncio.gather(*remote_cleanups, return_exceptions=True)
        self._remote_session_cleanups.clear()
        await self.scheduler.close()

    async def _cleanup_orphaned_opencode_sessions(
        self, *, terminal: bool
    ) -> None:
        """Retry deletion of checkpointed OpenCode sessions without replacing them."""
        if self.opencode is None:
            return
        jobs = self.storage.list_jobs()
        for job in jobs:
            if job.state.is_terminal is not terminal:
                continue
            for checkpoint_key in _OPENCODE_CHECKPOINT_KEYS:
                session_id = job.checkpoint.get(checkpoint_key)
                if not isinstance(session_id, str) or not session_id.strip():
                    continue
                cleaned = await self._bounded_remote_session_cleanup(
                    job.job_id, checkpoint_key, session_id.strip()
                )
                if not cleaned:
                    # A hard wait deadline must not cancel-and-await arbitrary
                    # cleanup code: cancellation handlers may themselves block.
                    # Close the recoverable job before the scheduler starts so
                    # no replacement session can race the still-owned resource.
                    if not terminal:
                        self._fail_closed_remote_session_owner(job.job_id)
                    continue
                refreshed = self.storage.get_job(job.job_id)
                if refreshed is not None:
                    job = refreshed

    async def _bounded_remote_session_cleanup(
        self, job_id: str, checkpoint_key: str, session_id: str
    ) -> bool:
        ownership = (job_id, checkpoint_key, session_id)
        task = self._remote_session_cleanups.get(ownership)
        if task is None:
            task = asyncio.create_task(
                self._delete_checkpointed_remote_session(
                    job_id, checkpoint_key, session_id
                ),
                name=f"poo-ia-opencode-cleanup-{job_id[:8]}",
            )
            self._remote_session_cleanups[ownership] = task

            def forget(completed: asyncio.Task[bool]) -> None:
                if self._remote_session_cleanups.get(ownership) is completed:
                    self._remote_session_cleanups.pop(ownership, None)

            task.add_done_callback(forget)
        done, _ = await asyncio.wait(
            (task,), timeout=OPENCODE_RECOVERY_CLEANUP_TIMEOUT_SECONDS
        )
        return task.result() if task in done else False

    async def _delete_checkpointed_remote_session(
        self, job_id: str, checkpoint_key: str, session_id: str
    ) -> bool:
        if self.opencode is None:
            return False
        try:
            await self.opencode.cleanup_session(session_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            return False
        current = self.storage.get_job(job_id)
        if current is None or current.checkpoint.get(checkpoint_key) != session_id:
            return True
        self.storage.remove_job_checkpoint_keys(job_id, (checkpoint_key,))
        return True

    def _fail_closed_remote_session_owner(self, job_id: str) -> None:
        current = self.storage.get_job(job_id)
        if current is None or current.state.is_terminal:
            return
        target = (
            JobStatus.CANCELLED
            if current.state is JobStatus.QUEUED
            else JobStatus.FAILED
        )
        self.storage.transition_job(
            job_id,
            target,
            message=OPENCODE_RECOVERY_FAILED_MESSAGE,
            safe_error=OPENCODE_RECOVERY_FAILED_MESSAGE,
        )

    def _schedule_terminal_session_cleanup(self) -> None:
        if self.opencode is None:
            return
        self._terminal_session_cleanup_requested = True
        current = self._terminal_session_cleanup
        if current is not None and not current.done():
            return
        self._terminal_session_cleanup = asyncio.create_task(
            self._run_terminal_session_cleanup(),
            name="poo-ia-opencode-terminal-cleanup",
        )

    async def _run_terminal_session_cleanup(self) -> None:
        while self._terminal_session_cleanup_requested:
            self._terminal_session_cleanup_requested = False
            await self._cleanup_orphaned_opencode_sessions(terminal=True)

    def set_repositories(self, repositories: Iterable[str]) -> tuple[str, ...]:
        """Atomically replace the safe inventory reported by the host worker."""
        updated = tuple(
            dict.fromkeys(
                repository.strip()
                for repository in repositories
                if isinstance(repository, str) and repository.strip()
            )
        )
        self.repositories = updated
        return updated

    async def handle(self, message: InboundMessage) -> Submission:
        """Accept one message, returning quickly when it creates heavy work.

        Every user-facing result is placed in the durable outbox. The transport
        should flush pending parts after this method returns and keep a background
        dispatcher waiting on :meth:`wait_for_output` for later job results.
        """
        await self.start()
        key = message.conversation_key
        async with self.memory.lock(key):
            snapshot = self.memory.snapshot(key)
            decision = route_message(
                message.text,
                active_repository=snapshot.conversation.active_repository,
                active_job_id=snapshot.conversation.last_job_id,
                repositories=self.repositories,
            )

            active_repository = decision.repository
            if decision.intent is Intent.RESEARCH and active_repository is None:
                active_repository = self._mentioned_repository(message.text)
                if active_repository is None:
                    active_repository = snapshot.conversation.active_repository
            if is_documentation_repository(active_repository):
                active_repository = None

            job_kind: JobKind | None = None
            job_payload: dict[str, Any] | None = None
            target_job: Job | None = None
            combined_change_pr = (
                decision.intent is Intent.PULL_REQUEST
                and explicitly_requests_code_change(message.text)
            )

            if decision.intent is Intent.RESEARCH:
                job_kind = JobKind.RESEARCH
                job_payload = {
                    "question": message.text,
                    "conversation_context": snapshot.rendered,
                    "active_repository": active_repository,
                }
            elif decision.intent is Intent.CODE_CHANGE:
                job_kind = JobKind.CODEX
                job_payload = self._code_payload(
                    message.text,
                    snapshot.rendered,
                    decision.repository,
                    publish=False,
                )
            elif decision.intent is Intent.PULL_REQUEST:
                if combined_change_pr:
                    # This is a new mutation plus publication, even when the
                    # preceding active job happened to be research.
                    fresh = route_message(
                        message.text,
                        active_repository=snapshot.conversation.active_repository,
                        active_job_id=None,
                        repositories=self.repositories,
                    )
                    active_repository = fresh.repository
                    if active_repository is not None:
                        job_kind = JobKind.CODEX
                        job_payload = self._code_payload(
                            message.text,
                            snapshot.rendered,
                            active_repository,
                            publish=True,
                        )
                else:
                    target_job = self._resolve_job(decision.job_id)
                    codex_target = self._codex_target(target_job)
                    if codex_target is not None:
                        target_job = codex_target
                        explicit_targets = mentioned_execution_repositories(message.text, self.repositories)
                        if is_documentation_repository(target_job.repository):
                            decision = RouteDecision(Intent.CLARIFY, Backend.NONE, reason=DOCUMENTATION_REPOSITORY_READ_ONLY)
                        elif explicit_targets and target_job.repository not in explicit_targets:
                            decision = RouteDecision(Intent.CLARIFY, Backend.NONE, reason="REPOSITORY_JOB_MISMATCH")
                        else:
                            job_kind = JobKind.PUBLISH
                            job_payload = {
                                "target_job_id": target_job.job_id,
                                "override": self._requests_override(message.text),
                            }
                            active_repository = target_job.repository

            registration = self.storage.register_inbound(
                message,
                intent=decision.intent,
                backend=decision.backend,
                job_kind=job_kind,
                repository=active_repository,
                payload=job_payload,
            )
            if not registration.created:
                if (
                    registration.job is None
                    and not self.storage.request_has_outbox(
                        registration.request.request_id
                    )
                    and registration.request.status is not RequestStatus.COMPLETED
                ):
                    await self._process_registered_direct(
                        message,
                        registration.request,
                        decision,
                        snapshot.rendered,
                        snapshot.conversation.active_repository,
                    )
                if self.outbox.pending(key):
                    self._output_available.set()
                return Submission(
                    registration.request.request_id,
                    False,
                    registration.request.intent,
                    registration.request.job_id,
                    queued=(
                        registration.job is not None
                        and registration.job.state
                        in {JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.PUBLISHING}
                    ),
                )
            request_id = registration.request.request_id
            if registration.job is None:
                await self._process_registered_direct(
                    message,
                    registration.request,
                    decision,
                    snapshot.rendered,
                    snapshot.conversation.active_repository,
                )
            else:
                self.outbox.enqueue_progress(
                    request_id,
                    f"Trabajo {registration.job.job_id[:8]} en cola.",
                    dedupe_key="queued",
                )
                self._output_available.set()
                self.scheduler.enqueue(registration.job.job_id)

            return Submission(
                request_id,
                True,
                decision.intent,
                None if registration.job is None else registration.job.job_id,
                queued=registration.job is not None,
            )

    async def _recover_direct_message(self, message: InboundMessage) -> None:
        """Finish one request persisted before its direct response existed."""
        key = message.conversation_key
        async with self.memory.lock(key):
            registration = self.storage.register_inbound(message)
            request = registration.request
            if (
                registration.job is not None
                or request.status is RequestStatus.COMPLETED
                or self.storage.request_has_outbox(request.request_id)
            ):
                return
            snapshot = self.memory.snapshot(key)
            decision = route_message(
                message.text,
                active_repository=snapshot.conversation.active_repository,
                active_job_id=snapshot.conversation.last_job_id,
                repositories=self.repositories,
            )
            try:
                await self._process_registered_direct(
                    message,
                    request,
                    decision,
                    snapshot.rendered,
                    snapshot.conversation.active_repository,
                )
            except Exception:
                if not self.storage.request_has_outbox(request.request_id):
                    self.storage.update_request_status(
                        request.request_id, RequestStatus.FAILED
                    )
                    self._enqueue_direct(
                        request.request_id,
                        PROCESSING_FAILED_MESSAGE,
                        backend=Backend.STORAGE,
                        remember=False,
                    )

    async def _process_registered_direct(
        self,
        message: InboundMessage,
        request: InboundRequest,
        decision: RouteDecision,
        conversation_context: str,
        active_repository: str | None,
    ) -> None:
        """Complete a no-job request using its persisted intent as authority."""
        if self.storage.request_has_outbox(request.request_id):
            return
        intent = request.intent
        if intent is None or intent in {Intent.RESEARCH, Intent.CODE_CHANGE}:
            self.storage.update_request_status(request.request_id, RequestStatus.FAILED)
            self._enqueue_direct(
                request.request_id,
                PROCESSING_FAILED_MESSAGE,
                backend=Backend.STORAGE,
                remember=False,
            )
            return

        self.storage.update_request_status(request.request_id, RequestStatus.PROCESSING)
        if intent is Intent.FORGET:
            self.memory.forget(message.conversation_key)
            self._enqueue_direct(
                request.request_id,
                "He olvidado la conversación y también reinicié el repositorio y trabajo activos.",
                remember=False,
            )
        elif intent is Intent.JOB_STATUS:
            text = await self._status_text(self._resolve_job(decision.job_id))
            self._enqueue_direct(request.request_id, text, remember=False)
        elif intent is Intent.CANCEL:
            text = await self._cancel_text(self._resolve_job(decision.job_id))
            self._enqueue_direct(request.request_id, text, remember=False)
        elif intent is Intent.AWS_REPORT:
            self._enqueue_direct(
                request.request_id, AWS_DISABLED_MESSAGE, remember=False
            )
        elif intent is Intent.CLARIFY:
            text = {
                AMBIGUOUS_REPOSITORY: AMBIGUOUS_REPOSITORY_MESSAGE,
                DOCUMENTATION_REPOSITORY_READ_ONLY: (
                    "El brain es una fuente de documentación, no un repositorio de ejecución. "
                    "Indica el servicio que debo modificar; el proceso se documentará después en el brain."
                ),
                UNKNOWN_REPOSITORY: "Ese repositorio no está disponible para ejecución. Indica uno del inventario del worker.",
                "REPOSITORY_JOB_MISMATCH": "El trabajo indicado pertenece a otro repositorio. El PR debe publicarse en su repositorio de ejecución original.",
            }.get(decision.reason, REPOSITORY_REQUIRED_MESSAGE)
            self._enqueue_direct(
                request.request_id, text, backend=Backend.NONE, remember=True
            )
        elif intent is Intent.PULL_REQUEST:
            text = (
                REPOSITORY_REQUIRED_MESSAGE
                if explicitly_requests_code_change(message.text)
                else JOB_NOT_FOUND_MESSAGE
            )
            self._enqueue_direct(request.request_id, text, remember=False)
        elif intent is Intent.CHAT:
            await self._handle_chat(
                request.request_id,
                message.text,
                conversation_context,
                active_repository,
            )
        elif intent is Intent.CAPABILITIES:
            text = (
                "Puedo leer la documentación del brain y preparar cambios en un repositorio de ejecución "
                "mediante Codex. Indica el servicio y el cambio; el PR se crea cuando lo solicitas. "
                "Después registro el proceso en un worktree separado del brain."
                if self.worker is not None
                else "Puedo consultar la documentación del brain, pero el worker de cambios no está configurado."
            )
            self._enqueue_direct(request.request_id, text, remember=False)

    async def record_processing_failure(
        self,
        message: InboundMessage,
        *,
        safe_message: str = PROCESSING_FAILED_MESSAGE,
    ) -> bool:
        """Persist an adapter-level failure without duplicating accepted work.

        Returns ``True`` when this call owns a durable error response. If the
        original handler already created output or a job, that durable state is
        kept as the source of truth and merely woken for recovery.
        """
        await self.start()
        registration = self.storage.register_inbound(
            message,
            backend=Backend.STORAGE,
        )
        request = registration.request
        job = registration.job
        if job is not None:
            self.scheduler.enqueue(job.job_id)
            if self.outbox.pending(message.conversation_key):
                self._output_available.set()
            return False
        if self.storage.request_has_outbox(request.request_id):
            self._output_available.set()
            return False
        if request.status is RequestStatus.COMPLETED:
            return False
        if request.status is not RequestStatus.FAILED:
            self.storage.update_request_status(
                request.request_id, RequestStatus.FAILED
            )
        self.outbox.enqueue(
            request.request_id,
            safe_message,
            backend=Backend.STORAGE,
            kind="error",
            dedupe_key="processing-failed",
            remember_exchange=False,
        )
        self._output_available.set()
        return True

    def pending_outputs(
        self, key: ConversationKey | None = None
    ) -> list[OutboxPart]:
        pending = self.outbox.pending(key)
        if not pending and key is None:
            self._output_available.clear()
        return pending

    def acknowledge_output(
        self, part: OutboxPart, discord_message_id: str | int
    ) -> OutboxPart:
        acknowledged = self.outbox.acknowledge(part, discord_message_id)
        if not self.outbox.pending():
            self._output_available.clear()
        return acknowledged

    async def flush_outputs(
        self,
        sender: Callable[[OutboxPart], Awaitable[object]],
        *,
        key: ConversationKey | None = None,
    ) -> int:
        count = await self.outbox.flush(sender, key=key)
        if not self.outbox.pending():
            self._output_available.clear()
        return count

    async def wait_for_output(self, timeout: float | None = None) -> bool:
        """Wait for immediate or background output without losing a wake-up race."""
        if self.outbox.pending():
            return True
        self._output_available.clear()
        if self.outbox.pending():
            self._output_available.set()
            return True
        try:
            if timeout is None:
                await self._output_available.wait()
            else:
                await asyncio.wait_for(self._output_available.wait(), timeout=timeout)
        except TimeoutError:
            return False
        return bool(self.outbox.pending())

    async def _handle_chat(
        self,
        request_id: str,
        text: str,
        conversation_context: str,
        active_repository: str | None,
    ) -> None:
        prompt = build_prompt(
            self.instructions,
            text,
            conversation_context=conversation_context,
            active_repository=active_repository,
        )
        try:
            result = await self.ollama.generate(prompt)
        except Exception:
            self.storage.update_request_status(request_id, RequestStatus.FAILED)
            self._enqueue_direct(
                request_id, GENERATION_FAILED_MESSAGE, backend=Backend.OLLAMA, remember=False
            )
            return
        self._enqueue_direct(
            request_id, result, backend=Backend.OLLAMA, remember=True
        )

    def _enqueue_direct(
        self,
        request_id: str,
        text: str,
        *,
        backend: Backend | None = Backend.STORAGE,
        remember: bool,
    ) -> None:
        self.outbox.enqueue(
            request_id,
            text,
            backend=backend,
            remember_exchange=remember,
        )
        if not remember:
            current = self.storage.get_inbound(request_id)
            if current is not None and current.status is not RequestStatus.FAILED:
                self.storage.update_request_status(request_id, RequestStatus.COMPLETED)
        self._output_available.set()

    def _code_payload(
        self,
        text: str,
        conversation_context: str,
        repository: str | None,
        *,
        publish: bool,
    ) -> dict[str, Any]:
        return {
            "prompt": text,
            "conversation_context": conversation_context,
            "repository": repository,
            "publish": publish,
            "skip_preflight": "sin investigar" in _normalized_words(text),
            # Snapshot trusted rules/personality with the durable local job so
            # recovery cannot silently substitute a newer policy payload.
            "policy": self.instructions,
        }

    async def _execute_heavy_job(self, job: Job) -> JobOutcome:
        if job.kind is JobKind.RESEARCH:
            if self.opencode is None:
                raise RuntimeError(OPENCODE_DISABLED_MESSAGE)
            result = await self._run_checkpointed_research(
                job,
                str(job.payload.get("question", "")),
                instructions=self.instructions,
                conversation_context=str(job.payload.get("conversation_context", "")),
                active_repository=job.payload.get("active_repository"),
                checkpoint_key="opencode_session_id",
            )
            return JobOutcome(JobStatus.SUCCEEDED, summary=result, checkpoint={})

        if job.kind is JobKind.CODEX:
            if self.worker is None:
                raise RuntimeError(WORKER_DISABLED_MESSAGE)
            repository = job.repository or str(job.payload.get("repository") or "")
            if not repository:
                raise RuntimeError(REPOSITORY_REQUIRED_MESSAGE)
            if is_documentation_repository(repository):
                raise RuntimeError("El brain es documental y no puede recibir trabajos de ejecución. Indica el servicio.")
            prompt = str(job.payload.get("prompt", ""))
            try:
                existing = await self.worker.get_job(job.job_id)
            except WorkerNotFoundError:
                existing = None
            if existing is not None:
                return await self._wait_for_worker(
                    job.job_id,
                    existing,
                    wait_for_publication=bool(job.payload.get("publish")),
                )

            preflight = ""
            target_files: tuple[str, ...] = ()
            if not bool(job.payload.get("skip_preflight")):
                if self.opencode is None:
                    raise RuntimeError(OPENCODE_DISABLED_MESSAGE)
                preflight = await self._run_checkpointed_research(
                    job,
                    build_preflight_request(prompt, repository),
                    instructions=self.instructions,
                    conversation_context=str(
                        job.payload.get("conversation_context", "")
                    ),
                    active_repository=repository,
                    checkpoint_key="preflight_session_id",
                )
                evidence = parse_preflight(preflight, repository)
                target_files = evidence.files
                preflight = evidence.to_context()
            manifest = await self.worker.create_codex_job(
                job_id=job.job_id,
                repository=repository,
                prompt=prompt,
                preflight=preflight,
                policy=str(job.payload.get("policy", self.instructions)),
                publish=bool(job.payload.get("publish")),
                target_files=target_files,
            )
            return await self._wait_for_worker(
                job.job_id,
                manifest,
                wait_for_publication=bool(job.payload.get("publish")),
            )

        if job.kind is JobKind.PUBLISH:
            if self.worker is None:
                raise RuntimeError(WORKER_DISABLED_MESSAGE)
            target_id = str(job.payload.get("target_job_id", ""))
            target = self.storage.get_job(target_id)
            if target is None:
                raise RuntimeError(JOB_NOT_FOUND_MESSAGE)
            if is_documentation_repository(target.repository):
                raise RuntimeError("El PR de ejecución no puede publicarse en el brain documental.")
            notification_was_completed = target.notification_completed_at is not None
            manifest = await self.worker.publish_job(
                target_id, override=bool(job.payload.get("override"))
            )
            target_outcome = await self._wait_for_worker(
                target_id,
                manifest,
                preserve_notification_ack=notification_was_completed,
            )
            if target_outcome.state is JobStatus.PREPARED:
                return JobOutcome(
                    JobStatus.FAILED,
                    summary=target_outcome.summary,
                    safe_error=(
                        target_outcome.safe_error
                        or "La publicación no terminó y el cambio sigue preparado."
                    ),
                    branch=target_outcome.branch,
                    checkpoint=target_outcome.checkpoint,
                    validation_status=target_outcome.validation_status,
                )
            return JobOutcome(
                target_outcome.state,
                summary=target_outcome.summary,
                safe_error=target_outcome.safe_error,
                branch=target_outcome.branch,
                external_reference=target_outcome.external_reference,
                checkpoint=target_outcome.checkpoint,
                validation_status=target_outcome.validation_status,
            )

        raise RuntimeError(f"unsupported heavy job kind: {job.kind.value}")

    async def _run_checkpointed_research(
        self,
        job: Job,
        prompt: str,
        *,
        checkpoint_key: str,
        **kwargs: object,
    ) -> str:
        """Recover one OpenCode session before starting a replacement."""
        if self.opencode is None:
            raise RuntimeError(OPENCODE_DISABLED_MESSAGE)
        previous = job.checkpoint.get(checkpoint_key)
        if isinstance(previous, str) and previous.strip():
            await self.opencode.cleanup_session(previous)
            self._set_remote_session(job.job_id, checkpoint_key, None)

        def checkpoint(session_id: str) -> None:
            self._set_remote_session(job.job_id, checkpoint_key, session_id)

        result = await self.opencode.research(
            prompt,
            on_session_created=checkpoint,
            **kwargs,
        )
        try:
            self._set_remote_session(job.job_id, checkpoint_key, None)
        except (InvalidJobTransition, KeyError):
            # Cancellation or terminal reconciliation already won.
            pass
        return result

    def _set_remote_session(
        self, job_id: str, checkpoint_key: str, session_id: str | None
    ) -> None:
        current = self.storage.get_job(job_id)
        if current is None:
            raise KeyError(job_id)
        checkpoint = dict(current.checkpoint)
        if session_id is None:
            checkpoint.pop(checkpoint_key, None)
        else:
            checkpoint[checkpoint_key] = session_id
        self.storage.update_running_job_checkpoint(job_id, checkpoint)

    async def _wait_for_worker(
        self,
        job_id: str,
        manifest: Mapping[str, object],
        *,
        wait_for_publication: bool = False,
        preserve_notification_ack: bool = False,
    ) -> JobOutcome:
        if self.worker is None:
            raise RuntimeError(WORKER_DISABLED_MESSAGE)
        prepared_observations = 0
        while True:
            state = self._manifest_state(manifest)
            if state is JobStatus.PREPARED and wait_for_publication:
                prepared_observations += 1
                # The detached worker records PREPARED just before its automatic
                # publication step. One extra poll closes that observable race;
                # a second unchanged PREPARED state is a recoverable publication
                # gate/failure and is returned to the owner.
                if manifest.get("error") is not None or prepared_observations >= 2:
                    self._synchronize_worker_manifest(
                        job_id,
                        manifest,
                        preserve_notification_ack=preserve_notification_ack,
                    )
                    return self._outcome_from_manifest(manifest)
            elif state in {
                JobStatus.PREPARED,
                JobStatus.SUCCEEDED,
                JobStatus.FAILED,
                JobStatus.CANCELLED,
            }:
                self._synchronize_worker_manifest(
                    job_id,
                    manifest,
                    preserve_notification_ack=preserve_notification_ack,
                )
                return self._outcome_from_manifest(manifest)
            else:
                prepared_observations = 0
                self._synchronize_worker_manifest(
                    job_id,
                    manifest,
                    preserve_notification_ack=preserve_notification_ack,
                )
            await asyncio.sleep(self.worker_poll_seconds)
            manifest = await self.worker.get_job(job_id)

    @staticmethod
    def _manifest_state(manifest: Mapping[str, object]) -> JobStatus:
        raw_state = manifest.get("state", manifest.get("status"))
        try:
            return JobStatus(str(raw_state))
        except ValueError as error:
            raise RuntimeError("El worker devolvió un estado desconocido.") from error

    def _outcome_from_manifest(self, manifest: Mapping[str, object]) -> JobOutcome:
        state = self._manifest_state(manifest)
        if state in {JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.PUBLISHING}:
            raise RuntimeError("El worker todavía no tiene un resultado final.")
        return JobOutcome(state, **self._manifest_details(manifest))

    @staticmethod
    def _manifest_details(manifest: Mapping[str, object]) -> dict[str, Any]:
        validation = manifest.get("validation")
        validation_status = None
        if isinstance(validation, Mapping) and validation.get("status") is not None:
            try:
                validation_status = ValidationStatus(str(validation["status"]))
            except ValueError:
                validation_status = None
        checkpoint_keys = (
            "repository",
            "repo_path",
            "base_commit",
            "worktree",
            "branch",
            "codex_exit_code",
            "validation",
            "diff",
            "pr_url",
            "result_path",
            "documentation",
        )
        checkpoint = {
            key: manifest[key]
            for key in checkpoint_keys
            if key in manifest and manifest[key] is not None
        }
        return {
            "summary": (
                str(manifest["summary"])
                if manifest.get("summary") is not None
                else None
            ),
            "safe_error": (
                str(manifest["error"]) if manifest.get("error") is not None else None
            ),
            "branch": (
                str(manifest["branch"]) if manifest.get("branch") is not None else None
            ),
            "external_reference": (
                str(manifest["pr_url"]) if manifest.get("pr_url") is not None else None
            ),
            "checkpoint": checkpoint,
            "validation_status": validation_status,
        }

    def _synchronize_worker_manifest(
        self,
        job_id: str,
        manifest: Mapping[str, object],
        *,
        preserve_notification_ack: bool = False,
    ) -> Job | None:
        current = self.storage.get_job(job_id)
        if current is None or current.state.is_terminal:
            return current
        raw_state = manifest.get("state", manifest.get("status"))
        try:
            target = JobStatus(str(raw_state))
        except ValueError:
            return current
        # Publication is reversible remotely: GitHub failures intentionally move
        # PUBLISHING back to PREPARED. Keep that transient state out of SQLite so
        # the local, forward-only state machine can accept either final outcome.
        if target in {JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.PUBLISHING}:
            return current
        details = self._manifest_details(manifest)

        if current.state is JobStatus.RUNNING and target is JobStatus.PUBLISHING:
            path = (JobStatus.PREPARED, JobStatus.PUBLISHING)
        elif current.state is JobStatus.PREPARED and target is JobStatus.SUCCEEDED:
            path = (JobStatus.PUBLISHING, JobStatus.SUCCEEDED)
        elif current.state is JobStatus.QUEUED and target is JobStatus.PREPARED:
            path = (JobStatus.RUNNING, JobStatus.PREPARED)
        elif current.state is JobStatus.QUEUED and target is JobStatus.PUBLISHING:
            path = (JobStatus.RUNNING, JobStatus.PREPARED, JobStatus.PUBLISHING)
        elif current.state is JobStatus.QUEUED and target is JobStatus.SUCCEEDED:
            path = (JobStatus.RUNNING, JobStatus.SUCCEEDED)
        elif current.state is JobStatus.QUEUED and target is JobStatus.FAILED:
            path = (JobStatus.RUNNING, JobStatus.FAILED)
        elif target == current.state:
            return current
        else:
            path = (target,)

        for index, state in enumerate(path):
            final = index == len(path) - 1
            current = self.storage.transition_job(
                job_id,
                state,
                message="Estado del worker sincronizado.",
                branch=details["branch"] if final else current.branch,
                external_reference=(
                    details["external_reference"] if final else current.external_reference
                ),
                checkpoint=details["checkpoint"] if final else current.checkpoint,
                summary=details["summary"] if final else current.summary,
                safe_error=details["safe_error"] if final else current.safe_error,
                validation_status=(
                    details["validation_status"] if final else current.validation_status
                ),
                preserve_notification_ack=preserve_notification_ack,
            )
        return current

    async def _cancel_external_job(self, job: Job) -> None:
        if job.kind is JobKind.RESEARCH:
            if self.opencode is not None:
                await self.opencode.abort_active()
            return
        if self.worker is None:
            raise RuntimeError(WORKER_DISABLED_MESSAGE)
        target_id = (
            str(job.payload.get("target_job_id"))
            if job.kind is JobKind.PUBLISH
            else job.job_id
        )
        manifest = await self.worker.cancel_job(target_id)
        remote_state = self._manifest_state(manifest)
        if job.kind is JobKind.CODEX:
            self._synchronize_worker_manifest(job.job_id, manifest)
        elif job.kind is JobKind.PUBLISH:
            self._synchronize_worker_manifest(target_id, manifest)
            if remote_state not in {JobStatus.CANCELLED, JobStatus.PREPARED}:
                raise RuntimeError(
                    f"El trabajo remoto terminó o cambió a {remote_state.value} antes de cancelarse."
                )

    async def _status_text(self, job: Job | None) -> str:
        if job is None:
            return JOB_NOT_FOUND_MESSAGE
        refresh_warning = ""
        if self.worker is not None and job.kind in {JobKind.CODEX, JobKind.PUBLISH}:
            target_id = (
                str(job.payload.get("target_job_id"))
                if job.kind is JobKind.PUBLISH
                else job.job_id
            )
            try:
                manifest = await self.worker.get_job(target_id)
                self._synchronize_worker_manifest(target_id, manifest)
                job = self.storage.get_job(job.job_id) or job
            except Exception:
                refresh_warning = " No pude actualizar el estado remoto; muestro el último guardado."
        elapsed = max(0, int(self._clock() - job.created_at))
        details = [
            f"Trabajo {job.job_id[:8]}: {job.state.value}.",
            f"Tiempo transcurrido: {elapsed}s.",
        ]
        if job.repository:
            details.append(f"Repositorio: {job.repository}.")
        if job.summary:
            details.append(job.summary)
        if job.safe_error:
            details.append(f"Error: {job.safe_error}")
        details.extend(self._documentation_details(job))
        return " ".join(details) + refresh_warning

    async def _cancel_text(self, job: Job | None) -> str:
        if job is None:
            return JOB_NOT_FOUND_MESSAGE
        if job.state.is_terminal:
            return f"El trabajo {job.job_id[:8]} ya terminó con estado {job.state.value}."
        try:
            cancelled = await self.scheduler.cancel(job.job_id)
        except Exception as error:
            remote = await self._remote_cancel_result(job)
            if remote is not None:
                return remote
            return f"No pude cancelar el trabajo {job.job_id[:8]}: {_safe_text(error)}"
        return self._format_cancel_result(cancelled)

    async def _on_job_finished(self, job: Job) -> None:
        if any(
            isinstance(job.checkpoint.get(key), str)
            and bool(str(job.checkpoint.get(key)).strip())
            for key in _OPENCODE_CHECKPOINT_KEYS
        ):
            # A live request can fail after persisting its session but before
            # its final delete succeeds. Trigger cleanup now; do not wait for a
            # future Discord message or process restart.
            self._schedule_terminal_session_cleanup()
        if job.state is JobStatus.SUCCEEDED and job.kind is JobKind.RESEARCH:
            text = job.summary or "La investigación terminó sin contenido."
            backend = Backend.OPENCODE
            remember = True
            explicit_repository = self._mentioned_repository(str(job.payload.get("question", "")))
            inferred_repository = explicit_repository or self._repository_from_research(text)
            if inferred_repository is not None:
                self.storage.set_conversation_context_for_request(
                    job.request_id, active_repository=inferred_repository
                )
        elif job.state in {JobStatus.PREPARED, JobStatus.SUCCEEDED}:
            text = self._format_change_result(job)
            backend = Backend.WORKER
            remember = True
        elif job.state is JobStatus.CANCELLED:
            text = f"El trabajo {job.job_id[:8]} fue cancelado."
            backend = Backend.STORAGE
            remember = False
        else:
            detail = f" Detalle: {job.safe_error}" if job.safe_error else ""
            text = f"El trabajo {job.job_id[:8]} falló.{detail}"
            if job.repository:
                text += f"\nRepositorio de ejecución: {job.repository}."
            if job.summary:
                text += "\n" + job.summary[:2500]
            backend = Backend.WORKER if job.kind is not JobKind.RESEARCH else Backend.OPENCODE
            remember = False
            self.storage.update_request_status(job.request_id, RequestStatus.FAILED)
        if job.state in {JobStatus.FAILED, JobStatus.CANCELLED}:
            details = self._documentation_details(job)
            if details:
                text += "\n" + "\n".join(details)
        self.outbox.enqueue(
            job.request_id,
            text,
            backend=backend,
            remember_exchange=remember,
        )
        self._output_available.set()

    @staticmethod
    def _format_change_result(job: Job) -> str:
        parts = [f"Trabajo {job.job_id[:8]} {job.state.value}."]
        if job.repository:
            parts.append(f"Repositorio: {job.repository}.")
        if job.branch:
            parts.append(f"Rama: {job.branch}.")
        if job.validation_status:
            parts.append(f"Validación: {job.validation_status.value}.")
        if job.summary:
            parts.append(job.summary)
        if job.external_reference:
            parts.append(f"PR: {job.external_reference}")
        parts.extend(PooIAOrchestrator._documentation_details(job))
        if job.state is JobStatus.PREPARED:
            parts.append("El cambio quedó aislado y listo para revisión o publicación.")
        return "\n".join(parts)

    @staticmethod
    def _documentation_details(job: Job) -> list[str]:
        parts: list[str] = []
        documentation = job.checkpoint.get("documentation")
        if isinstance(documentation, Mapping):
            if documentation.get("state") == "prepared":
                parts.append(
                    f"Bitácora del brain preparada por separado: {documentation.get('path')}. "
                    f"Rama: {documentation.get('branch')}. Pendiente de integrar al brain."
                )
            elif documentation.get("state") == "failed":
                parts.append(f"Documentación pendiente: {documentation.get('error')}.")
        return parts

    def _resolve_job(self, reference: str | None) -> Job | None:
        if not reference:
            return None
        exact = self.storage.get_job(reference)
        if exact is not None:
            return exact
        normalized = reference.casefold()
        matches = [
            job for job in self.storage.list_jobs() if job.job_id.casefold().startswith(normalized)
        ]
        return matches[0] if len(matches) == 1 else None

    def _codex_target(self, job: Job | None) -> Job | None:
        """Unwrap publication wrappers until reaching their original Codex job."""
        seen: set[str] = set()
        current = job
        while current is not None and current.job_id not in seen:
            seen.add(current.job_id)
            if current.kind is JobKind.CODEX:
                return current
            if current.kind is not JobKind.PUBLISH:
                return None
            target_id = str(current.payload.get("target_job_id") or "")
            current = self.storage.get_job(target_id) if target_id else None
        return None

    async def _remote_cancel_result(self, job: Job) -> str | None:
        if self.worker is None or job.kind not in {JobKind.CODEX, JobKind.PUBLISH}:
            return None
        target = self._codex_target(job)
        if target is None:
            return None
        try:
            manifest = await self.worker.get_job(target.job_id)
        except Exception:
            return None
        self._synchronize_worker_manifest(target.job_id, manifest)
        state = self._manifest_state(manifest)
        parts = [
            f"No se canceló el trabajo {job.job_id[:8]}; el estado remoto real es {state.value}."
        ]
        if manifest.get("pr_url"):
            parts.append(f"PR: {manifest['pr_url']}")
        return " ".join(parts)

    @staticmethod
    def _format_cancel_result(job: Job) -> str:
        if job.state is JobStatus.CANCELLED:
            return f"Trabajo {job.job_id[:8]} cancelado."
        parts = [
            f"No se canceló el trabajo {job.job_id[:8]}; terminó con estado {job.state.value}."
        ]
        if job.external_reference:
            parts.append(f"PR: {job.external_reference}")
        return " ".join(parts)

    def _mentioned_repository(self, text: str) -> str | None:
        matches = mentioned_execution_repositories(text, self.repositories)
        return matches[0] if len(matches) == 1 else None

    def _repository_from_research(self, summary: str) -> str | None:
        """Infer one service repo from cited research, never from ambiguity.

        ``brain-capnet`` is supporting documentation rather than the target of a
        normal service change, so it does not compete with exactly one cited
        service repository.
        """
        normalized_summary = f" {_normalized_words(summary)} "
        mentioned = [
            repository
            for repository in self.repositories
            if f" {_normalized_words(repository)} " in normalized_summary
        ]
        service_repositories = [
            repository
            for repository in mentioned
            if not is_documentation_repository(repository)
        ]
        candidates = service_repositories
        return candidates[0] if len(candidates) == 1 else None

    @staticmethod
    def _requests_override(text: str) -> bool:
        normalized = _normalized_words(text)
        return "a pesar" in normalized or "aunque fallen" in normalized or "forzar" in normalized


# Concise name for adapters and tests.
Orchestrator = PooIAOrchestrator
