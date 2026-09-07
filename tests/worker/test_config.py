from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from worker.config import (
    DEFAULT_OPERATIONAL_RETENTION_DAYS,
    DEFAULT_RETENTION_SWEEP_SECONDS,
    WorkerConfigurationError,
    WorkerSettings,
)


class WorkerSettingsTests(unittest.TestCase):
    def base_environment(self) -> dict[str, str]:
        return {"WORKER_PASSWORD": "a-private-password-longer-than-16"}

    def test_defaults_are_loopback_and_secret_is_not_in_repr(self) -> None:
        settings = WorkerSettings.from_environment(self.base_environment())

        self.assertEqual(settings.host, "127.0.0.1")
        self.assertEqual(settings.port, 4097)
        self.assertEqual(settings.max_changed_files, 5)
        self.assertNotIn(settings.password, repr(settings))
        self.assertEqual(settings.jobs_root, settings.data_root / "jobs")
        self.assertEqual(
            settings.operational_retention_days,
            DEFAULT_OPERATIONAL_RETENTION_DAYS,
        )
        self.assertEqual(
            settings.retention_sweep_seconds,
            DEFAULT_RETENTION_SWEEP_SECONDS,
        )

    def test_accepts_ipv6_loopback(self) -> None:
        environment = self.base_environment() | {"WORKER_HOST": "::1"}
        self.assertEqual(WorkerSettings.from_environment(environment).host, "::1")

    def test_rejects_non_loopback_bind(self) -> None:
        environment = self.base_environment() | {"WORKER_HOST": "0.0.0.0"}
        with self.assertRaisesRegex(WorkerConfigurationError, "loopback"):
            WorkerSettings.from_environment(environment)

    def test_rejects_weak_password_relative_paths_and_invalid_port(self) -> None:
        cases = (
            ({"WORKER_PASSWORD": "short"}, "at least 16"),
            (self.base_environment() | {"CAPNET_WORKSPACE": "relative"}, "absolute"),
            (self.base_environment() | {"WORKER_PORT": "70000"}, "65535"),
            (self.base_environment() | {"WORKER_USERNAME": "bad:name"}, "invalid"),
        )
        for environment, message in cases:
            with self.subTest(environment=environment):
                with self.assertRaisesRegex(WorkerConfigurationError, message):
                    WorkerSettings.from_environment(environment)

    def test_cloud_configuration_has_no_credential_or_endpoint_fields(self) -> None:
        field_names = set(WorkerSettings.__dataclass_fields__)
        self.assertFalse(any(
            any(part in name for part in ("access_key", "secret_key", "session_token", "endpoint"))
            for name in field_names
        ))

    def test_enables_isolated_validation_and_host_aws_queries(self) -> None:
        settings = WorkerSettings.from_environment(self.base_environment() | {
            "VALIDATION_ENABLED": "true", "AWS_ENABLED": "true",
            "AWS_PROFILE": "default", "AWS_REGION": "us-east-1",
            "AWS_QUERY_TIMEOUT_SECONDS": "15", "VALIDATION_BUILD_TIMEOUT_SECONDS": "300",
        })
        self.assertTrue(settings.validation_enabled)
        self.assertTrue(settings.aws_enabled)
        self.assertEqual(settings.aws_profile, "default")
        self.assertEqual(settings.aws_region, "us-east-1")
        self.assertEqual(settings.aws_query_timeout_seconds, 15)
        self.assertEqual(settings.validation_build_timeout_seconds, 300)

    def test_rejects_invalid_validation_and_aws_options(self) -> None:
        for name, value in (("VALIDATION_ENABLED", "maybe"), ("AWS_ENABLED", "maybe"),
                            ("AWS_PROFILE", "--endpoint-url"), ("AWS_REGION", "bad region"),
                            ("AWS_QUERY_TIMEOUT_SECONDS", "nan"),
                            ("VALIDATION_BUILD_TIMEOUT_SECONDS", "0")):
            with self.subTest(name=name), self.assertRaisesRegex(WorkerConfigurationError, name):
                WorkerSettings.from_environment(self.base_environment() | {name: value})

    def test_accepts_worker_retention_configuration(self) -> None:
        settings = WorkerSettings.from_environment(
            self.base_environment()
            | {
                "OPERATIONAL_RETENTION_DAYS": "45",
                "WORKER_RETENTION_SWEEP_SECONDS": "7200",
            }
        )

        self.assertEqual(settings.operational_retention_days, 45)
        self.assertEqual(settings.retention_sweep_seconds, 7200)

    def test_business_tables_are_explicit_host_configuration(self) -> None:
        defaults = WorkerSettings.from_environment(self.base_environment())
        self.assertIsNone(defaults.aws_tasks_table)
        self.assertIsNone(defaults.aws_dealer_config_table)
        settings = WorkerSettings.from_environment(self.base_environment() | {
            "AWS_TASKS_TABLE": "tasks-v2-prod",
            "AWS_DEALER_CONFIG_TABLE": "dealer-config-prod",
        })
        self.assertEqual(settings.aws_tasks_table, "tasks-v2-prod")
        self.assertEqual(settings.aws_dealer_config_table, "dealer-config-prod")
        for name in ("AWS_TASKS_TABLE", "AWS_DEALER_CONFIG_TABLE"):
            for invalid in ("file:///private", "Tasks; whoami", "x" * 256):
                with self.subTest(name=name, invalid=invalid), self.assertRaises(WorkerConfigurationError):
                    WorkerSettings.from_environment(self.base_environment() | {name: invalid})

    def test_rejects_invalid_retention_and_overlapping_resolved_roots(self) -> None:
        with self.assertRaisesRegex(WorkerConfigurationError, "positive"):
            WorkerSettings.from_environment(
                self.base_environment() | {"OPERATIONAL_RETENTION_DAYS": "0"}
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            alias = root / "workspace-alias"
            alias.symlink_to(workspace, target_is_directory=True)
            environment = self.base_environment() | {
                "CAPNET_WORKSPACE": str(workspace),
                "CAPNET_WORKTREES": str(alias),
                "WORKER_DATA_ROOT": str(root / "data"),
            }
            with self.assertRaisesRegex(WorkerConfigurationError, "disjoint"):
                WorkerSettings.from_environment(environment)


if __name__ == "__main__":
    unittest.main()
