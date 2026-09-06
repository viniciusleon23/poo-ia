"""Record the separate documentation stage without concealing code outcomes."""

from dataclasses import asdict

from .config import WorkerSettings
from .models import JobManifest


def record_process(settings: WorkerSettings, manifest: JobManifest) -> dict[str, str]:
    try:
        from .brain_journal import BrainJournal
        return asdict(BrainJournal(settings).record(manifest))
    except Exception:
        # Provider output and repository-controlled errors can contain secrets.
        # Preserve the execution result and expose a separate, retryable stage.
        return {
            "state": "failed",
            "error": "No se pudo preparar la bitácora separada del brain; revisa su disponibilidad y worktree documental",
        }
