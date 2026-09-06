"""Detached entry point for one durable Codex job."""

from __future__ import annotations

import argparse
import os

from .codex_runner import CodexRunner
from .config import WorkerSettings
from .github import GitHubPublisher, PublicationError
from .models import JobState
from .publication import finish_publication_request
from .retention import job_lease
from .store import ManifestStore


def _load_detached_settings() -> WorkerSettings:
    """Load non-HTTP settings without requiring the API's shared credential."""
    detached_environment = os.environ.copy()
    detached_environment["WORKER_PASSWORD"] = "unused-by-detached-worker"
    return WorkerSettings.from_environment(detached_environment)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--publish-only", action="store_true")
    parser.add_argument("--override", action="store_true")
    parser.add_argument("job_id")
    arguments = parser.parse_args()

    # Detached children never serve the authenticated HTTP API. Their launcher
    # deliberately removes its shared password, so satisfy the unified settings
    # parser with a process-local mapping rather than restoring any credential
    # to this process environment.
    settings = _load_detached_settings()
    # The detached job does not serve HTTP. Do not propagate the shared API
    # credential to Codex, Git, tests, or GitHub child processes.
    os.environ.pop("WORKER_PASSWORD", None)
    store = ManifestStore(settings.jobs_root)
    with job_lease(settings.jobs_root, arguments.job_id):
        if arguments.publish_only:
            try:
                GitHubPublisher(settings, store).publish_claimed(
                    arguments.job_id,
                    override=arguments.override,
                    process_pid=os.getpid(),
                )
            except PublicationError:
                # The manifest is restored to PREPARED with a safe retryable error.
                return
            finally:
                finish_publication_request(settings.jobs_root, arguments.job_id)
            return
        runner = CodexRunner(settings, store)
        runner.run(arguments.job_id, process_pid=os.getpid())
        manifest = store.get(arguments.job_id)
        if manifest.requested_publish and manifest.state is JobState.PREPARED:
            try:
                GitHubPublisher(settings, store).publish(
                    arguments.job_id, process_pid=os.getpid()
                )
            except PublicationError:
                # The durable manifest contains the safe, retryable publication error.
                return


if __name__ == "__main__":
    main()
