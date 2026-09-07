"""Operational feedback based on confirmed stages, without model-generated claims."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable
from contextlib import asynccontextmanager

from .models import Intent, JobStatus
from .outbox import DurableOutbox
from .storage import SQLiteStorage


LOGGER = logging.getLogger(__name__)
PHASES = {
    "queued": "esperando turno para comenzar",
    "research": "consultando la documentación del brain",
    "preflight": "revisando la documentación del brain para ubicar el cambio",
    "prepare": "preparando el repositorio de ejecución",
    "edit": "el agente está trabajando en el cambio solicitado",
    "validate": "ejecutando las pruebas y revisando los cambios",
    "document": "registrando el proceso en el brain",
    "publish": "preparando y publicando el PR",
    "execution": "el worker está procesando el cambio",
    "worker_wait": "esperando que el worker inicie el trabajo",
}


class ProgressReporter:
    def __init__(
        self, storage: SQLiteStorage, outbox: DurableOutbox, wake: Callable[[], None],
        *, clock: Callable[[], float] = time.time, interval_seconds: float = 60,
        receipt_delay_seconds: float = 0.5,
    ) -> None:
        if not math.isfinite(interval_seconds) or interval_seconds <= 0:
            raise ValueError("Progress interval must be positive and finite")
        if not math.isfinite(receipt_delay_seconds) or receipt_delay_seconds < 0:
            raise ValueError("Receipt delay must be nonnegative and finite")
        self.storage, self.outbox, self.wake = storage, outbox, wake
        self.clock, self.interval_seconds = clock, interval_seconds
        self.receipt_delay_seconds = receipt_delay_seconds
        self._monitor: asyncio.Task | None = None
        self._direct_tasks: set[asyncio.Task] = set()
        self._heartbeat_keys: dict[str, str] = {}
        self._phase_keys: dict[str, str] = {}
        self._pending_phases: dict[str, str] = {}

    def start(self) -> None:
        if self._monitor is None or self._monitor.done():
            self._monitor = asyncio.create_task(self._run(), name="poo-ia-progress")

    async def close(self) -> None:
        tasks = list(self._direct_tasks)
        if self._monitor is not None:
            tasks.append(self._monitor)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._direct_tasks.clear()
        self._monitor = None

    def phase(self, job_id: str, phase: str) -> None:
        if not isinstance(phase, str) or phase not in PHASES:
            return
        self._pending_phases[job_id] = phase
        try:
            self._persist_phase(job_id, phase)
        except Exception:
            # Observability must never cancel or orphan the operation it reports.
            LOGGER.warning("Could not persist a work stage; feedback will retry.")
        else:
            self._pending_phases.pop(job_id, None)

    def _persist_phase(self, job_id: str, phase: str) -> None:
        job = self.storage.get_job(job_id)
        if job is None or job.state not in {JobStatus.RUNNING, JobStatus.PUBLISHING}:
            return
        if self.storage.request_has_final_outbox(job.request_id):
            return
        checkpoint = dict(job.checkpoint)
        revision = checkpoint.get("progress_revision", 0)
        if checkpoint.get("progress_phase") != phase:
            revision = revision + 1 if isinstance(revision, int) else 1
            checkpoint.update(progress_phase=phase, progress_at=self.clock(), progress_revision=revision)
            self.storage.update_running_job_checkpoint(job_id, checkpoint)
        key = f"phase-{revision}-{phase}"
        if self._phase_keys.get(job_id) != key:
            self._emit(job.request_id, f"Trabajo {job_id[:8]}: {PHASES[phase]}.", key)
            self._phase_keys[job_id] = key

    def emit_heartbeats(self) -> None:
        for job_id, phase in tuple(self._pending_phases.items()):
            self.phase(job_id, phase)
        now = self.clock()
        jobs = self.storage.list_jobs(states=(JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.PUBLISHING))
        active_ids = {job.job_id for job in jobs}
        self._heartbeat_keys = {key: value for key, value in self._heartbeat_keys.items() if key in active_ids}
        self._phase_keys = {key: value for key, value in self._phase_keys.items() if key in active_ids}
        for job in jobs:
            if self.storage.request_has_final_outbox(job.request_id):
                continue
            phase = "queued" if job.state is JobStatus.QUEUED else job.checkpoint.get("progress_phase", "execution")
            if not isinstance(phase, str) or phase not in PHASES:
                phase = "execution"
            last = job.checkpoint.get("progress_at", job.created_at)
            if not isinstance(last, (int, float)) or not math.isfinite(last):
                last = job.created_at
            if now - last < self.interval_seconds:
                continue
            key = f"waiting-{job.checkpoint.get('progress_revision', 0)}-{phase}-{int((now - last) // self.interval_seconds)}"
            if self._heartbeat_keys.get(job.job_id) == key:
                continue
            self._emit(
                job.request_id,
                f"Trabajo {job.job_id[:8]}: sigo pendiente del resultado. "
                f"Última etapa confirmada: {PHASES[phase]}. Te avisaré al terminar o si aparece un bloqueo.",
                key,
            )
            self._heartbeat_keys[job.job_id] = key

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(min(1.0, self.interval_seconds))
            try:
                self.emit_heartbeats()
            except Exception:
                LOGGER.warning("Could not persist progress; will retry on the next interval.")

    @asynccontextmanager
    async def direct(self, request_id: str, intent: Intent | None):
        task = None
        if intent in {Intent.CHAT, Intent.AWS_REPORT, Intent.JOB_STATUS, Intent.CANCEL}:
            task = asyncio.create_task(self._direct_wait(request_id, intent), name="poo-ia-receipt")
            self._direct_tasks.add(task)
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                self._direct_tasks.discard(task)

    async def _direct_wait(self, request_id: str, intent: Intent) -> None:
        await asyncio.sleep(self.receipt_delay_seconds)
        texts = {
            Intent.AWS_REPORT: "Recibí tu consulta. Estoy consultando AWS en modo de solo lectura; te enviaré el resultado al terminar.",
            Intent.CHAT: "Recibí tu mensaje. Estoy preparando la respuesta.",
            Intent.JOB_STATUS: "Estoy consultando el estado del trabajo.",
            Intent.CANCEL: "Recibí la solicitud de cancelación. Estoy confirmando el estado del trabajo.",
        }
        count = 0
        while not self.storage.request_has_final_outbox(request_id):
            try:
                if count == 0:
                    self.outbox.enqueue(request_id, texts[intent], kind="ack", dedupe_key="received", remember_exchange=False)
                    self.wake()
                else:
                    self._emit(request_id, "Sigo esperando la respuesta del servicio. Aún no tengo un resultado confirmado.", f"direct-wait-{count}")
            except Exception:
                LOGGER.warning("Could not persist receipt/progress feedback.")
            count += 1
            await asyncio.sleep(self.interval_seconds)

    def _emit(self, request_id: str, text: str, key: str) -> None:
        self.outbox.enqueue_progress(request_id, text, dedupe_key=key, now=self.clock())
        self.wake()
