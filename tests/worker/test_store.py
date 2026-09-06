from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from worker.models import JobRequest, JobState
from worker.store import (
    IdempotencyConflictError,
    ManifestStateError,
    ManifestStore,
)


class ManifestStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "jobs"
        self.store = ManifestStore(self.root)
        self.request = JobRequest(
            "discord-123",
            "capnet-service",
            "Agrega task_available.",
            "Schema path was identified.",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_create_is_idempotent_and_survives_reopen(self) -> None:
        first, created = self.store.create_or_get(self.request)
        reopened = ManifestStore(self.root)
        second, repeated_created = reopened.create_or_get(self.request)

        self.assertTrue(created)
        self.assertFalse(repeated_created)
        self.assertEqual(first, second)
        self.assertEqual((self.root / "discord-123.json").stat().st_mode & 0o777, 0o600)

    def test_same_id_with_different_payload_conflicts(self) -> None:
        self.store.create_or_get(self.request)
        changed = JobRequest("discord-123", "capnet-service", "Otro cambio")

        with self.assertRaises(IdempotencyConflictError):
            self.store.create_or_get(changed)

    def test_expected_state_prevents_late_update_after_cancel(self) -> None:
        self.store.create_or_get(self.request)
        self.store.update(
            self.request.job_id,
            expected=(JobState.QUEUED,),
            transform=lambda item: item.evolve(state=JobState.CANCELLED),
        )

        with self.assertRaises(ManifestStateError):
            self.store.update(
                self.request.job_id,
                expected=(JobState.RUNNING,),
                transform=lambda item: item.evolve(state=JobState.PREPARED),
            )

    def test_public_manifest_omits_prompt_preflight_and_hash(self) -> None:
        manifest, _ = self.store.create_or_get(self.request)
        public = manifest.to_public_dict()
        self.assertNotIn("prompt", public)
        self.assertNotIn("preflight", public)
        self.assertNotIn("policy", public)
        self.assertNotIn("payload_hash", public)

        stored = json.loads((self.root / "discord-123.json").read_text())
        self.assertEqual(stored["state"], "queued")

    def test_old_manifest_without_new_optional_fields_still_loads(self) -> None:
        self.store.create_or_get(self.request)
        path = self.root / "discord-123.json"
        stored = json.loads(path.read_text(encoding="utf-8"))
        stored.pop("policy")
        stored.pop("base_branch")
        stored.pop("diff_sha256")
        stored.pop("terminal_at")
        path.write_text(json.dumps(stored), encoding="utf-8")

        loaded = ManifestStore(self.root).get("discord-123")

        self.assertEqual(loaded.policy, "")
        self.assertIsNone(loaded.base_branch)
        self.assertIsNone(loaded.diff_sha256)
        self.assertIsNone(loaded.terminal_at)
        _, created = self.store.create_or_get(self.request)
        self.assertFalse(created)

    def test_trusted_policy_participates_in_idempotency_hash(self) -> None:
        request = JobRequest(
            "discord-124",
            "capnet-service",
            "Agrega task_available.",
            policy="Preserve public defaults.",
        )
        self.store.create_or_get(request)

        with self.assertRaises(IdempotencyConflictError):
            self.store.create_or_get(
                JobRequest(
                    "discord-124",
                    "capnet-service",
                    "Agrega task_available.",
                    policy="Change public defaults.",
                )
            )

    def test_rejects_path_like_job_id(self) -> None:
        with self.assertRaises(ValueError):
            JobRequest("../escape", "repo", "change")

    def test_terminal_timestamp_and_conditional_fsynced_deletion(self) -> None:
        self.store.create_or_get(self.request)
        finished = datetime(2026, 7, 1, tzinfo=UTC)
        manifest = self.store.update(
            self.request.job_id,
            expected=(JobState.QUEUED,),
            transform=lambda item: item.evolve(
                state=JobState.SUCCEEDED,
                updated_at=finished.isoformat(),
            ),
        )

        self.assertEqual(manifest.terminal_at, finished.isoformat())
        self.assertFalse(
            self.store.delete_if_expired_terminal(
                manifest.job_id,
                payload_hash=manifest.payload_hash,
                cutoff=finished - timedelta(seconds=1),
            )
        )
        self.assertFalse(
            self.store.delete_if_expired_terminal(
                manifest.job_id,
                payload_hash="different",
                cutoff=finished + timedelta(seconds=1),
            )
        )
        self.assertTrue(
            self.store.delete_if_expired_terminal(
                manifest.job_id,
                payload_hash=manifest.payload_hash,
                cutoff=finished,
            )
        )
        self.assertFalse((self.root / f"{manifest.job_id}.json").exists())

    def test_scan_isolates_corrupt_manifests(self) -> None:
        manifest, _ = self.store.create_or_get(self.request)
        (self.root / "corrupt.json").write_text("not json", encoding="utf-8")

        scan = self.store.scan()

        self.assertEqual(scan.manifests, (manifest,))
        self.assertEqual(len(scan.failures), 1)
        self.assertEqual(scan.failures[0].job_id, "corrupt")


if __name__ == "__main__":
    unittest.main()
