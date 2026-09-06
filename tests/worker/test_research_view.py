from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from worker.research_view import (
    ResearchViewError,
    _export_repository,
    build_research_view,
)


class ResearchViewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.source = root / "workspace"
        self.target = root / "research-view"
        self.source.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _repository(self, name: str) -> Path:
        repository = self.source / name
        repository.mkdir()
        subprocess.run(["git", "init", "-q", repository], check=True)
        subprocess.run(
            ["git", "-C", repository, "config", "user.name", "Test"], check=True
        )
        subprocess.run(
            ["git", "-C", repository, "config", "user.email", "test@example.com"],
            check=True,
        )
        return repository

    @staticmethod
    def _commit(repository: Path) -> None:
        subprocess.run(["git", "-C", repository, "add", "."], check=True)
        subprocess.run(
            ["git", "-C", repository, "commit", "-qm", "fixture"], check=True
        )

    def test_exports_only_committed_safe_regular_text(self) -> None:
        brain = self._repository("brain-capnet")
        (brain / "ai").mkdir()
        (brain / "ai" / "rutas-de-consulta.md").write_text("rutas", encoding="utf-8")
        (brain / "ai" / "catalog.json").write_text(
            '{"database_password": "PlaintextPassword123"}\n', encoding="utf-8"
        )
        (brain / "ai" / "leak.md").write_text(
            "Database password: PlaintextPassword456\n", encoding="utf-8"
        )
        (brain / ".env").write_text("TOKEN=tracked", encoding="utf-8")
        (brain / "private.pem").write_text("key", encoding="utf-8")
        (brain / "image.png").write_bytes(b"\x89PNG")
        (brain / "link.md").symlink_to("/etc/passwd")
        (brain / "Solicitudes").mkdir()
        (brain / "Solicitudes" / "restringida.md").write_text(
            "solo con autorización", encoding="utf-8"
        )
        self._commit(brain)
        (brain / "untracked.md").write_text("do not export", encoding="utf-8")

        service = self._repository("service-one")
        (service / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        (service / "config.yaml").write_text(
            "database_url: postgresql://admin:PlaintextPassword123@db/service\n",
            encoding="utf-8",
        )
        (service / "payment.py").write_text(
            'PAYMENT_TOKEN = "sk_' 'live_1234567890abcdef"\n', encoding="utf-8"
        )
        (service / "auth.ts").write_text(
            'const DATABASE_PASSWORD = "PlaintextPassword789";\n',
            encoding="utf-8",
        )
        (service / "startup.sh").write_text(
            "export DATABASE_PASSWORD=PlaintextPassword987\n", encoding="utf-8"
        )
        (service / "short_password.py").write_text(
            'PASSWORD = "hunter2"\n', encoding="utf-8"
        )
        (service / "phrase_password.py").write_text(
            'PASSWORD = "short secret phrase"\n', encoding="utf-8"
        )
        (service / "kwarg.py").write_text(
            'client = connect(password="short secret phrase")\n', encoding="utf-8"
        )
        (service / "mapping.py").write_text(
            'config["password"] = "short secret phrase"\n'
            'os.environ["API_TOKEN"] = "another short secret"\n',
            encoding="utf-8",
        )
        (service / "placeholder_prefix.py").write_text(
            'client = connect(password="secret horse battery staple")\n',
            encoding="utf-8",
        )
        (service / "short_dsn.py").write_text(
            'DSN = "postgresql://admin:abc@db.internal/service"\n',
            encoding="utf-8",
        )
        (service / "safe_secret_example.py").write_text(
            'API_TOKEN = "placeholder"\n', encoding="utf-8"
        )
        self._commit(service)
        (service / "app.py").write_text("VALUE = 2\n", encoding="utf-8")

        exported = build_research_view(self.source, self.target)

        self.assertEqual(exported, ("brain-capnet", "service-one"))
        self.assertEqual(
            (self.target / "brain-capnet/ai/rutas-de-consulta.md").read_text(),
            "rutas",
        )
        self.assertEqual((self.target / "service-one/app.py").read_text(), "VALUE = 1\n")
        self.assertFalse((self.target / "service-one/config.yaml").exists())
        self.assertFalse((self.target / "service-one/payment.py").exists())
        self.assertFalse((self.target / "service-one/auth.ts").exists())
        self.assertFalse((self.target / "service-one/startup.sh").exists())
        self.assertFalse((self.target / "service-one/short_password.py").exists())
        self.assertFalse((self.target / "service-one/phrase_password.py").exists())
        self.assertFalse((self.target / "service-one/kwarg.py").exists())
        self.assertFalse((self.target / "service-one/mapping.py").exists())
        self.assertFalse(
            (self.target / "service-one/placeholder_prefix.py").exists()
        )
        self.assertFalse((self.target / "service-one/short_dsn.py").exists())
        self.assertTrue((self.target / "service-one/safe_secret_example.py").exists())
        self.assertFalse((self.target / "brain-capnet/ai/catalog.json").exists())
        self.assertFalse((self.target / "brain-capnet/ai/leak.md").exists())
        self.assertFalse((self.target / "brain-capnet/.env").exists())
        self.assertFalse((self.target / "brain-capnet/private.pem").exists())
        self.assertFalse((self.target / "brain-capnet/image.png").exists())
        self.assertFalse((self.target / "brain-capnet/link.md").exists())
        self.assertFalse(
            (self.target / "brain-capnet/Solicitudes/restringida.md").exists()
        )
        self.assertFalse((self.target / "brain-capnet/untracked.md").exists())
        self.assertFalse(any(path.name == ".git" for path in self.target.rglob("*")))

    def test_rejects_overlapping_roots_and_missing_brain(self) -> None:
        with self.assertRaises(ResearchViewError):
            build_research_view(self.source, self.source / "view")
        self._repository("service-only")
        with self.assertRaisesRegex(ResearchViewError, "brain-capnet"):
            build_research_view(self.source, self.target)

    def test_ignores_first_level_repository_symlinks_outside_workspace(self) -> None:
        brain = self._repository("brain-capnet")
        (brain / "Inicio.md").write_text("inicio", encoding="utf-8")
        self._commit(brain)

        outside = Path(self.temporary.name) / "outside-git"
        outside.mkdir()
        subprocess.run(["git", "init", "-q", outside], check=True)
        subprocess.run(
            ["git", "-C", outside, "config", "user.name", "Test"], check=True
        )
        subprocess.run(
            ["git", "-C", outside, "config", "user.email", "test@example.com"],
            check=True,
        )
        (outside / "steal.py").write_text("EXTERNAL = True\n", encoding="utf-8")
        self._commit(outside)
        (self.source / "external-service").symlink_to(outside, target_is_directory=True)

        exported = build_research_view(self.source, self.target)

        self.assertEqual(exported, ("brain-capnet",))
        self.assertFalse((self.target / "external-service").exists())

    def test_archive_timeout_kills_the_complete_process_group(self) -> None:
        repository = self._repository("brain-capnet")
        (repository / "Inicio.md").write_text("inicio", encoding="utf-8")
        self._commit(repository)
        real_git = shutil.which("git")
        assert real_git is not None
        fake_bin = Path(self.temporary.name) / "fake-bin"
        fake_bin.mkdir()
        fake_git = fake_bin / "git"
        fake_git.write_text(
            "#!/bin/sh\n"
            'if [ "$3" = archive ]; then\n'
            "  sleep 5\n"
            "  exit 0\n"
            "fi\n"
            f"exec {shlex.quote(real_git)} \"$@\"\n",
            encoding="utf-8",
        )
        fake_git.chmod(0o700)
        destination = Path(self.temporary.name) / "destination"
        destination.mkdir()

        started = time.monotonic()
        with patch.dict(
            os.environ,
            {"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
        ):
            with self.assertRaisesRegex(ResearchViewError, "timed out"):
                _export_repository(
                    repository, destination, timeout_seconds=0.05
                )

        self.assertLess(time.monotonic() - started, 1.0)
