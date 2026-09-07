from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from worker.processes import CommandResult, CommandTimedOut
from worker.validation_sandbox import DockerValidationRunner, ValidationSandboxError


class FakeDocker:
    def __init__(self) -> None:
        self.calls = []
        self.images = set()
        self.build_contexts = []
        self.snapshots = []
        self.test_result = CommandResult(0, "tests passed")
        self.build_result = CommandResult(0)

    def run(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        args = list(argv)[5:]
        if args[:2] == ["image", "inspect"]:
            return CommandResult(0 if args[2] in self.images else 1)
        if args[0] == "build":
            context = Path(args[-1])
            self.build_contexts.append({p.name: p.read_bytes() for p in context.iterdir()})
            self.images.add(args[args.index("--tag") + 1])
            return self.build_result
        if args[0] == "run":
            mount = args[args.index("--mount") + 1]
            stage = Path(mount.split("src=", 1)[1].split(",dst=", 1)[0])
            self.snapshots.append({str(p.relative_to(stage)): p.read_bytes() for p in stage.rglob("*") if p.is_file()})
            if isinstance(self.test_result, Exception):
                raise self.test_result
            return self.test_result
        if args[:2] == ["rm", "--force"]:
            return CommandResult(0)
        raise AssertionError(f"unexpected Docker command: {args}")


class ValidationSandboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.repository = self.root / "repository"
        self.repository.mkdir()
        (self.repository / "pyproject.toml").write_text(
            "[project]\nname='tasks'\nversion='0.1.0'\nrequires-python='>=3.13'\n"
            "[dependency-groups]\ndev=['pytest']\n", encoding="utf-8",
        )
        (self.repository / "uv.lock").write_text("version=1\n", encoding="utf-8")
        (self.repository / "source.py").write_text("value = True\n", encoding="utf-8")
        self.engine = FakeDocker()
        self.factory = DockerValidationRunner(
            staging_root=self.root / "worker-data/validation", runner=self.engine,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_build_receives_only_base_metadata_and_our_dockerfile_then_reuses_cache(self) -> None:
        (self.repository / "Dockerfile").write_text("RUN execute-untrusted-source\n")
        (self.repository / ".env").write_text("secret-canary")
        first = self.factory.prepare(self.repository)
        second = self.factory.prepare(self.repository)

        self.assertEqual(first.image, second.image)
        self.assertEqual(len(self.engine.build_contexts), 1)
        context = self.engine.build_contexts[0]
        self.assertEqual(set(context), {"pyproject.toml", "uv.lock", "Dockerfile"})
        self.assertNotIn(b"execute-untrusted-source", context["Dockerfile"])
        self.assertIn(b"--no-install-project", context["Dockerfile"])
        build = next(args for args, _ in self.engine.calls if "build" in args)
        self.assertIn("INCLUDE_DEV=1", build)
        self.assertIn("BASE_IMAGE=python:3.13-slim-bookworm", build)
        self.assertIn(b"FROM ghcr.io/astral-sh/uv:0.12.10 AS uv_binary", context["Dockerfile"])

    def test_metadata_and_python_changes_produce_distinct_images(self) -> None:
        first = self.factory.prepare(self.repository)
        (self.repository / "uv.lock").write_text("version=1\nrevision=3\n")
        second = self.factory.prepare(self.repository)
        (self.repository / ".python-version").write_text("3.14\n")
        third = self.factory.prepare(self.repository)
        self.assertEqual(len({first.image, second.image, third.image}), 3)

    def test_no_dev_group_does_not_request_a_nonexistent_group(self) -> None:
        (self.repository / "pyproject.toml").write_text("[project]\nname='tasks'\nversion='0.1'\n")
        self.factory.prepare(self.repository)
        build = next(args for args, _ in self.engine.calls if "build" in args)
        self.assertIn("INCLUDE_DEV=0", build)

    def test_test_container_has_no_network_host_credentials_or_original_checkout_mount(self) -> None:
        for name in (".aws", ".ssh", ".git", ".venv", "brain-capnet"):
            directory = self.repository / name
            directory.mkdir()
            (directory / "canary").write_text("must-not-enter-container")
        (self.repository / ".env").write_text("must-not-enter-container")
        prepared = self.factory.prepare(self.repository)
        with mock.patch.dict(os.environ, {"AWS_SECRET_ACCESS_KEY": "real-canary", "WORKER_PASSWORD": "worker-canary"}):
            result = prepared.run(("uv", "run", "--offline", "--no-sync", "python", "-m", "pytest"), cwd=self.repository, timeout=12)

        self.assertEqual(result.returncode, 0)
        argv, options = next(call for call in self.engine.calls if "run" in call[0])
        for flag in ("--read-only", "--rm", "--init", "--pull=never", "--pids-limit", "--cpus", "--memory"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--network") + 1], "none")
        self.assertEqual(argv[argv.index("--cap-drop") + 1], "ALL")
        self.assertIn("no-new-privileges=true", argv)
        self.assertIn("AWS_SECRET_ACCESS_KEY=testing", argv)
        self.assertIn("AWS_SHARED_CREDENTIALS_FILE=/dev/null", argv)
        self.assertNotIn("real-canary", str(argv))
        self.assertNotIn("worker-canary", str(argv))
        mount = argv[argv.index("--mount") + 1]
        self.assertIn(str(self.factory.staging_root), mount)
        self.assertNotIn(str(self.repository), mount)
        self.assertTrue(mount.endswith(",dst=/source,readonly"))
        self.assertIn(
            f"/workspace:rw,nosuid,nodev,size=536870912,mode=0700,uid={os.getuid()},gid={os.getgid()}", argv,
        )
        self.assertEqual(argv[argv.index("--log-driver") + 1], "none")
        self.assertIn("/opt/venv/bin/python", argv)
        self.assertEqual(argv[argv.index("--entrypoint") + 1], "/usr/bin/timeout")
        self.assertIn("12s", argv)
        self.assertEqual(options["timeout"], 27)
        self.assertEqual(set(self.engine.snapshots[0]), {"pyproject.toml", "uv.lock", "source.py"})
        self.assertEqual(list((self.factory.staging_root / "containers").glob("*.json")), [])

    def test_source_symlinks_are_rejected_without_following_them(self) -> None:
        outside = self.root / "outside-secret"
        outside.write_text("canary")
        (self.repository / "alias.py").symlink_to(outside)
        prepared = self.factory.prepare(self.repository)
        with self.assertRaisesRegex(ValidationSandboxError, "enlaces"):
            prepared.run(("python", "-m", "pytest"), cwd=self.repository)
        self.assertEqual(self.engine.snapshots, [])
        self.assertEqual(outside.read_text(), "canary")

    def test_changed_dependency_metadata_never_uses_stale_image(self) -> None:
        prepared = self.factory.prepare(self.repository)
        (self.repository / "uv.lock").write_text("version=1\nrevision=3\n")
        with self.assertRaisesRegex(ValidationSandboxError, "dependencias"):
            prepared.run(("python", "-m", "pytest"), cwd=self.repository)
        self.assertEqual(self.engine.snapshots, [])

    def test_build_failure_does_not_run_any_test_command(self) -> None:
        self.engine.build_result = CommandResult(1, stderr="build error with secret provider payload")
        with self.assertRaises(ValidationSandboxError) as raised:
            self.factory.prepare(self.repository)
        self.assertNotIn("secret provider payload", str(raised.exception))
        self.assertEqual(self.engine.snapshots, [])

    def test_test_timeout_and_docker_failure_always_request_container_cleanup(self) -> None:
        prepared = self.factory.prepare(self.repository)
        for result, expected in (
            (CommandResult(124), CommandTimedOut),
            (CommandTimedOut("CLI timeout"), CommandTimedOut),
            (CommandResult(125), ValidationSandboxError),
            (CommandResult(127), ValidationSandboxError),
        ):
            with self.subTest(result=result):
                self.engine.test_result = result
                with self.assertRaises(expected):
                    prepared.run(("python", "-m", "pytest"), cwd=self.repository, timeout=1)
                self.assertIn("rm", self.engine.calls[-1][0])
                self.assertEqual(list((self.factory.staging_root / "containers").glob("*.json")), [])

    def test_recovery_preserves_live_owner_identity_and_cleans_reused_pid(self) -> None:
        self.factory._initialize()
        name = "poo-ia-validation-" + "a" * 32
        path = self.factory.staging_root / "containers" / f"{name}.json"
        stage = self.factory.staging_root / f"run-{name}"
        stage.mkdir()
        path.write_text(json.dumps({
            "name": name, "repository": str(self.repository),
            "owner_pid": 123, "owner_identity": "boot:start1",
        }))
        with mock.patch("worker.validation_sandbox._process_identity", return_value="boot:start1"):
            self.factory.cleanup_orphans()
        self.assertTrue(path.exists())
        self.assertEqual(self.engine.calls, [])
        with mock.patch("worker.validation_sandbox._process_identity", return_value="boot:start2"):
            self.factory.cleanup_orphans()
        self.assertFalse(path.exists())
        self.assertFalse(stage.exists())
        self.assertIn(name, self.engine.calls[-1][0])

    def test_cancel_cleans_only_the_requested_repository(self) -> None:
        self.factory._initialize()
        records = []
        for suffix, repository in (("a", self.repository), ("b", self.root / "other")):
            name = "poo-ia-validation-" + suffix * 32
            path = self.factory.staging_root / "containers" / f"{name}.json"
            path.write_text(json.dumps({"name": name, "repository": str(repository)}))
            records.append(path)
        self.factory.cleanup_for_repository(self.repository)
        self.assertFalse(records[0].exists())
        self.assertTrue(records[1].exists())

    def test_unsupported_repository_is_unavailable_before_any_docker_execution(self) -> None:
        (self.repository / "uv.lock").unlink()
        with self.assertRaisesRegex(ValidationSandboxError, "uv.lock"):
            self.factory.prepare(self.repository)
        self.assertEqual(self.engine.calls, [])

    def test_invalid_python_version_and_symlinked_metadata_do_not_reach_docker(self) -> None:
        (self.repository / ".python-version").write_bytes(b"\xff")
        with self.assertRaises(ValidationSandboxError):
            self.factory.prepare(self.repository)
        (self.repository / ".python-version").unlink()
        project = self.repository / "pyproject.toml"
        project.unlink()
        project.symlink_to(self.root / "outside-secret")
        with self.assertRaises(ValidationSandboxError):
            self.factory.prepare(self.repository)
        self.assertEqual(self.engine.calls, [])

    def test_failed_cleanup_retains_checkpoint_for_recovery(self) -> None:
        prepared = self.factory.prepare(self.repository)
        with mock.patch.object(self.factory, "_remove_container", side_effect=OSError("daemon unavailable")):
            prepared.run(("python", "-m", "pytest"), cwd=self.repository)
        records = list((self.factory.staging_root / "containers").glob("*.json"))
        self.assertEqual(len(records), 1)
        record = json.loads(records[0].read_text())
        self.assertEqual(record["owner_pid"], os.getpid())
        self.assertTrue((self.factory.staging_root / f"run-{record['name']}").exists())
        self.factory.cleanup_for_repository(self.repository)
        self.assertFalse(records[0].exists())
