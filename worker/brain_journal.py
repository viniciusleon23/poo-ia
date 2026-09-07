"""Prepare deterministic process documentation in a separate brain worktree.

The journal never executes an agent, commits, or publishes. Its Markdown contains
only structured execution metadata, keeping prompts and model output out of the
documentation repository. The operational brain checkout remains untouched.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .config import WorkerSettings
from .models import JOB_ID_PATTERN, JobManifest, JobState
from .processes import CommandResult, CommandRunner, CommandTimedOut, SubprocessCommandRunner


class BrainJournalError(RuntimeError):
    """The execution outcome could not be documented in an isolated brain tree."""


@dataclass(frozen=True, slots=True)
class BrainJournalRecord:
    path: str
    branch: str
    state: str = "prepared"


class BrainJournal:
    def __init__(self, settings: WorkerSettings, runner: CommandRunner | None = None) -> None:
        self.settings = settings
        self.runner = runner or SubprocessCommandRunner()

    def record(self, manifest: JobManifest) -> BrainJournalRecord:
        """Prepare or refresh one job's documentation without changing its code."""
        if not JOB_ID_PATTERN.fullmatch(manifest.job_id):
            raise BrainJournalError("El identificador del trabajo no permite documentarlo.")
        if manifest.state not in {
            JobState.PREPARED, JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED,
        }:
            raise BrainJournalError("El trabajo aún no tiene un resultado para documentar.")

        brain = self.settings.workspace / "brain-capnet"
        worktrees = self.settings.worktrees_root
        target = worktrees / f"brain-docs-{manifest.job_id}"
        branch = f"poo-ia/docs-{manifest.job_id}"
        marker = f"<!-- poo-ia-job:{manifest.job_id} -->\n"
        try:
            _require_directory(self.settings.workspace)
            _require_directory(brain)
            git_marker = brain / ".git"
            if git_marker.is_symlink() or not git_marker.exists():
                raise BrainJournalError("brain-capnet no es un repositorio Git disponible.")
            actual_brain = self._git(brain, "rev-parse", "--show-toplevel").stdout.strip()
            if Path(actual_brain).resolve() != brain.resolve():
                raise BrainJournalError("brain-capnet no es la raíz de su repositorio Git.")
            common_dir = self._common_dir(brain)
            _require_directory(worktrees, create=True)
            if target.is_symlink():
                raise BrainJournalError("El worktree de documentación es un enlace simbólico.")
            if not target.exists():
                branch_exists = self._git(
                    brain, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False,
                ).returncode == 0
                if branch_exists:
                    raise BrainJournalError(
                        "La rama de documentación ya existe sin su worktree; requiere revisión."
                    )
                self._git(brain, "worktree", "add", "-b", branch, str(target), "HEAD")
            self._verify_worktree(target, branch, common_dir)

            directory = target
            for part in ("Procesos", "Poo-IA"):
                directory = directory / part
                _require_directory(directory, create=True)
            destination = directory / f"{manifest.job_id}.md"
            if destination.is_symlink() or (destination.exists() and not destination.is_file()):
                raise BrainJournalError("La ruta del documento no es un archivo regular seguro.")
            if destination.exists():
                if destination.stat().st_size > 64_000:
                    raise BrainJournalError("El documento existente requiere revisión antes de actualizarlo.")
                existing = destination.read_text(encoding="utf-8")
                if not existing.startswith(marker):
                    raise BrainJournalError("El documento existente no pertenece a este trabajo.")
            else:
                existing = None
            content = marker + _render(manifest)
            if content != existing:
                _atomic_write(destination, content)
            return BrainJournalRecord(str(destination.resolve()), branch)
        except BrainJournalError:
            raise
        except (OSError, UnicodeError, ValueError) as error:
            raise BrainJournalError("No se pudo preparar el documento del proceso en el brain.") from error

    def _verify_worktree(self, target: Path, branch: str, expected_common_dir: Path) -> None:
        _require_directory(target)
        if (target / ".git").is_symlink():
            raise BrainJournalError("El worktree de documentación contiene un enlace Git inesperado.")
        root = self._git(target, "rev-parse", "--show-toplevel").stdout.strip()
        actual_branch = self._git(target, "branch", "--show-current").stdout.strip()
        if (
            Path(root).resolve() != target.resolve()
            or actual_branch != branch
            or self._common_dir(target) != expected_common_dir
        ):
            raise BrainJournalError("El worktree existente no corresponde a la documentación del trabajo.")

    def _common_dir(self, path: Path) -> Path:
        directory = Path(self._git(path, "rev-parse", "--git-common-dir").stdout.strip())
        return (directory if directory.is_absolute() else path / directory).resolve()

    def _git(self, cwd: Path, *arguments: str, check: bool = True) -> CommandResult:
        try:
            result = self.runner.run(
                (
                    self.settings.git_executable,
                    "-c", "core.hooksPath=/dev/null",
                    "-c", "submodule.recurse=false",
                    *arguments,
                ),
                cwd=cwd,
                timeout=30.0,
            )
        except (OSError, CommandTimedOut) as error:
            raise BrainJournalError("Git no pudo preparar la documentación del brain.") from error
        if check and result.returncode != 0:
            # Git output and local paths may contain secrets; retain them only in
            # the underlying diagnostic exception, never in the public journal.
            raise BrainJournalError("Falló una operación Git del worktree de documentación.")
        return result


def _require_directory(path: Path, *, create: bool = False) -> None:
    if path.is_symlink():
        raise BrainJournalError("La ruta de documentación contiene un enlace simbólico.")
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir():
        raise BrainJournalError("La carpeta del brain o de su worktree no está disponible.")


def _atomic_write(destination: Path, content: str) -> None:
    descriptor, name = tempfile.mkstemp(prefix=".poo-ia-", suffix=".tmp", dir=destination.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _identifier(value: str | None) -> str:
    if value is None:
        return "No disponible"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,255}", value):
        return "Identificador no disponible"
    return f"`{value}`"


def _pull_request(value: str | None) -> str:
    if value is None:
        return "Todavía no publicado"
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "URL no disponible"
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or not re.fullmatch(r"[A-Za-z0-9.-]+", parsed.netloc)
        or not re.fullmatch(r"/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[0-9]+", parsed.path)
        or parsed.query
        or parsed.fragment
    ):
        return "URL no disponible"
    return f"[Ver PR]({value})"


def _render(manifest: JobManifest) -> str:
    lines = [
        f"# Registro de ejecución {manifest.job_id}",
        "",
        "Este registro documenta el resultado técnico de un trabajo de Poo-IA. "
        "El brain aporta documentación; los cambios de código y su PR pertenecen al repositorio de ejecución.",
        "",
        f"- Trabajo: `{manifest.job_id}`",
        f"- Repositorio de ejecución: {_identifier(manifest.repository)}",
        f"- Estado de ejecución: `{manifest.state.value}`",
        f"- Rama de ejecución: {_identifier(manifest.branch)}",
        f"- Commit base de ejecución: {_identifier(manifest.base_commit)}",
        f"- Pull request de ejecución: {_pull_request(manifest.pr_url)}",
    ]
    if manifest.diff is not None:
        lines.extend([
            f"- Archivos cambiados: {manifest.diff.changed_files}",
            f"- Líneas cambiadas: {manifest.diff.changed_lines}",
            f"- Archivos binarios: {len(manifest.diff.binary_files)}",
            f"- Presupuesto de cambios excedido: {'sí' if manifest.diff.over_budget else 'no'}",
        ])
    else:
        lines.append("- Diferencias: no disponibles")
    validation = manifest.validation
    if validation is None:
        lines.append("- Validación: no disponible; no implica pruebas aprobadas")
    else:
        lines.append(f"- Validación: `{validation.status.value}`")
        if validation.exit_code is not None:
            lines.append(f"- Código de salida de validación: {validation.exit_code}")
        if validation.baseline_exit_code is not None:
            lines.append(f"- Código de salida de validación en base: {validation.baseline_exit_code}")
    if manifest.state is JobState.FAILED:
        lines.append("- Resultado: el trabajo falló; el detalle operativo se conserva en el worker.")
    if manifest.state is JobState.CANCELLED:
        lines.append("- Resultado: trabajo cancelado.")
    lines.extend([
        "",
        "La documentación se prepara en una rama y un worktree separados del brain. "
        "Este registro no implica publicación, integración del PR ni despliegue del código.",
        "",
    ])
    return "\n".join(lines)
