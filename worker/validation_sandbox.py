"""Build trusted uv environments and run repository tests in disposable Docker containers.

Only dependency metadata from the base commit reaches the networked image build.
Tests receive a private source copy, synthetic AWS settings, and no network. Docker
is mandatory: failures never fall back to execution in the worker process.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import stat
import tempfile
import tomllib
import uuid
from pathlib import Path
from typing import Sequence

from .processes import CommandResult, CommandRunner, CommandTimedOut, SubprocessCommandRunner


class ValidationSandboxError(OSError):
    """A test environment is unavailable; this is not a repository test failure."""


_DOCKERFILE = """ARG BASE_IMAGE
FROM ghcr.io/astral-sh/uv:0.12.10 AS uv_binary
FROM ${BASE_IMAGE}
COPY --from=uv_binary /uv /uvx /usr/local/bin/
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy UV_NO_ENV_FILE=1
WORKDIR /opt/validation
COPY pyproject.toml uv.lock ./
ARG INCLUDE_DEV=0
RUN if [ "$INCLUDE_DEV" = 1 ]; then timeout --kill-after=10 540 uv sync --frozen --no-install-project --group dev; else timeout --kill-after=10 540 uv sync --frozen --no-install-project --no-dev; fi
WORKDIR /workspace
"""
_ENTRYPOINT = """import importlib.util, os, shutil, sys
os.makedirs('/tmp/home', exist_ok=True)
shutil.copytree('/source', '/workspace', dirs_exist_ok=True)
if 'pytest' in sys.argv[1:] and importlib.util.find_spec('pytest') is None:
    print('pytest is unavailable in the prepared image', file=sys.stderr)
    sys.exit(127)
try:
    os.execvpe(sys.argv[1], sys.argv[1:], dict(os.environ))
except FileNotFoundError:
    print('The isolated test executable is unavailable', file=sys.stderr)
    sys.exit(127)
"""
_NAME = re.compile(r"poo-ia-validation-[0-9a-f]{32}\Z")
_EXCLUDED = frozenset({
    ".git", ".gitconfig", ".git-credentials", ".aws", ".ssh", ".gnupg", ".docker",
    ".config", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".npmrc", ".pypirc", "node_modules", "credentials", "credentials.json",
    "auth.json", "id_rsa", "id_ed25519", "brain-capnet", "capnet-brain",
})
_SAFE_ENV = {
    "PATH": "/opt/venv/bin:/usr/local/bin:/usr/bin:/bin",
    "HOME": "/tmp/home",
    "LANG": "C.UTF-8",
    "ENV_NAME": "dev",
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_SESSION_TOKEN": "testing",
    "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_REGION": "us-east-1",
    "AWS_EC2_METADATA_DISABLED": "true",
    "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
    "AWS_CONFIG_FILE": "/dev/null",
    "UV_PROJECT_ENVIRONMENT": "/opt/venv",
    "UV_PYTHON_DOWNLOADS": "never",
    "UV_OFFLINE": "true",
    "UV_NO_ENV_FILE": "1",
    "UV_CACHE_DIR": "/tmp/uv-cache",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONUNBUFFERED": "1",
}


def _process_identity(pid: int) -> str | None:
    """Include boot and start ticks so a reused Linux PID cannot own a container."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip() + ":" + fields[19]
    except (OSError, IndexError):
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, ValueError):
            return None
        except PermissionError:
            pass
        return "alive-without-linux-identity"


def _metadata(repository: Path) -> tuple[dict[str, bytes], str, bool]:
    files: dict[str, bytes] = {}
    for name in ("pyproject.toml", "uv.lock"):
        path = repository / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 5_000_000:
            raise ValidationSandboxError("La validación aislada requiere pyproject.toml y uv.lock regulares.")
        files[name] = path.read_bytes()
    try:
        project = tomllib.loads(files["pyproject.toml"].decode())
        tomllib.loads(files["uv.lock"].decode())
    except (UnicodeError, ValueError) as error:
        raise ValidationSandboxError("Los metadatos uv del repositorio no son válidos.") from error
    python = "3.13"
    version_path = repository / ".python-version"
    if version_path.is_symlink():
        raise ValidationSandboxError(".python-version no puede ser un enlace simbólico.")
    if version_path.exists():
        if not version_path.is_file() or version_path.stat().st_size > 100:
            raise ValidationSandboxError(".python-version no contiene una versión simple 3.x.")
        files[".python-version"] = version_path.read_bytes()
        try:
            python = files[".python-version"].decode().strip()
        except UnicodeError as error:
            raise ValidationSandboxError(".python-version no contiene texto válido.") from error
        if not re.fullmatch(r"3\.[0-9]{1,2}", python):
            raise ValidationSandboxError("La imagen de validación admite .python-version en formato 3.x.")
    groups = project.get("dependency-groups", {})
    include_dev = isinstance(groups, dict) and "dev" in groups
    return files, python, include_dev


def _fingerprint(files: dict[str, bytes], python: str) -> str:
    digest = hashlib.sha256((_DOCKERFILE + "\0uv:0.12.10\0" + python).encode())
    for name, content in sorted(files.items()):
        digest.update(name.encode() + b"\0" + content + b"\0")
    return digest.hexdigest()


class DockerValidationRunner:
    def __init__(
        self,
        *,
        staging_root: Path,
        image_prefix: str = "poo-ia-validation",
        docker_executable: str = "docker",
        memory: str = "1g",
        cpus: float = 1.0,
        pids_limit: int = 128,
        build_timeout_seconds: float = 600.0,
        runner: CommandRunner | None = None,
    ) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9._/-]{0,127}", image_prefix):
            raise ValueError("image_prefix has an invalid format")
        if not re.fullmatch(r"[1-9][0-9]*[mg]", memory):
            raise ValueError("memory must be a positive Docker m/g limit")
        if not math.isfinite(cpus) or cpus <= 0 or pids_limit <= 0:
            raise ValueError("CPU and PID limits must be positive")
        if not math.isfinite(build_timeout_seconds) or build_timeout_seconds <= 0:
            raise ValueError("build_timeout_seconds must be positive")
        self.staging_root = Path(staging_root)
        self.image_prefix = image_prefix
        self.docker = docker_executable
        self.memory = memory
        self.cpus = cpus
        self.pids_limit = pids_limit
        self.build_timeout = build_timeout_seconds
        self.runner = runner or SubprocessCommandRunner()

    def _initialize(self) -> None:
        if self.staging_root.is_symlink():
            raise ValidationSandboxError("La carpeta de validación no puede ser un enlace simbólico.")
        self.staging_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name in ("containers", "docker-config"):
            path = self.staging_root / name
            if path.is_symlink():
                raise ValidationSandboxError("La carpeta de validación contiene un enlace simbólico.")
            path.mkdir(exist_ok=True, mode=0o700)

    def _docker(self, *args: str, timeout: float = 30.0) -> CommandResult:
        # The CLI receives no registry credentials from ~/.docker and always
        # targets the local daemon even if DOCKER_HOST is present on the host.
        return self.runner.run(
            (self.docker, "--host", "unix:///var/run/docker.sock", "--config",
             str(self.staging_root / "docker-config"), *args),
            timeout=timeout,
        )

    def prepare(self, repository: Path) -> "PreparedDockerValidationRunner":
        self._initialize()
        files, python, include_dev = _metadata(repository)
        fingerprint = _fingerprint(files, python)
        image = f"{self.image_prefix}:{fingerprint[:32]}"
        try:
            available = self._docker("image", "inspect", image)
            if available.returncode != 0:
                with tempfile.TemporaryDirectory(prefix="build-", dir=self.staging_root) as temporary:
                    context = Path(temporary)
                    for name, content in files.items():
                        (context / name).write_bytes(content)
                    (context / "Dockerfile").write_text(_DOCKERFILE, encoding="utf-8")
                    built = self._docker(
                        "build", "--tag", image,
                        "--build-arg", f"BASE_IMAGE=python:{python}-slim-bookworm",
                        "--build-arg", f"INCLUDE_DEV={int(include_dev)}",
                        str(context), timeout=self.build_timeout,
                    )
                    if built.returncode != 0:
                        raise ValidationSandboxError(
                            "No se pudo preparar la imagen uv del repositorio; revisa Docker, Python y las dependencias fijadas."
                        )
        except CommandTimedOut as error:
            raise ValidationSandboxError("La preparación de la imagen uv agotó su tiempo.") from error
        except OSError as error:
            if isinstance(error, ValidationSandboxError):
                raise
            raise ValidationSandboxError("Docker no está disponible para preparar las pruebas aisladas.") from error
        return PreparedDockerValidationRunner(self, image, fingerprint)

    def cleanup_for_repository(self, repository: Path) -> None:
        self._cleanup_records(repository=str(Path(repository).resolve()))

    def cleanup_orphans(self) -> None:
        self._cleanup_records(repository=None)

    def _cleanup_records(self, *, repository: str | None) -> None:
        self._initialize()
        for path in (self.staging_root / "containers").glob("*.json"):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                record = json.loads(path.read_text())
                name = record["name"]
                if not isinstance(name, str) or not _NAME.fullmatch(name) or path.stem != name:
                    continue
                if repository is not None:
                    if record.get("repository") != repository:
                        continue
                else:
                    pid = record.get("owner_pid")
                    if isinstance(pid, int) and pid > 0:
                        identity = _process_identity(pid)
                        if identity is not None and identity == record.get("owner_identity"):
                            continue
                self._remove_container(name)
                stage = self.staging_root / f"run-{name}"
                if stage.is_dir() and not stage.is_symlink():
                    shutil.rmtree(stage)
                path.unlink(missing_ok=True)
            except (OSError, ValueError, KeyError, TypeError, CommandTimedOut):
                # Retain the durable record for a later recovery attempt.
                continue

    def _remove_container(self, name: str) -> None:
        removed = self._docker("rm", "--force", name, timeout=15.0)
        if removed.returncode != 0:
            # A normally completed --rm container is already absent. Distinguish
            # that from an unreachable daemon before discarding its checkpoint.
            inspect = self._docker("container", "inspect", name, timeout=15.0)
            if inspect.returncode == 0 or "No such" not in (inspect.stderr + inspect.stdout):
                raise ValidationSandboxError("No se confirmó la limpieza del contenedor de pruebas.")


class PreparedDockerValidationRunner:
    def __init__(self, factory: DockerValidationRunner, image: str, fingerprint: str) -> None:
        self.factory = factory
        self.image = image
        self.fingerprint = fingerprint

    def run(self, argv: Sequence[str], *, cwd: Path, timeout: float | None = None) -> CommandResult:
        limit = 600.0 if timeout is None else timeout
        if not math.isfinite(limit) or limit <= 0:
            raise ValidationSandboxError("El tiempo de las pruebas debe estar limitado.")
        files, python, _ = _metadata(cwd)
        if _fingerprint(files, python) != self.fingerprint:
            raise ValidationSandboxError(
                "El cambio modifica las dependencias o Python; requiere preparar y revisar un entorno nuevo."
            )
        if not argv or any(not isinstance(arg, str) or "\0" in arg for arg in argv):
            raise ValidationSandboxError("El comando de pruebas no es válido.")
        factory = self.factory
        name = f"poo-ia-validation-{uuid.uuid4().hex}"
        stage = factory.staging_root / f"run-{name}"
        checkpoint = factory.staging_root / "containers" / f"{name}.json"
        try:
            record = {
                "name": name,
                "repository": str(Path(cwd).resolve()),
                "owner_pid": os.getpid(),
                "owner_identity": _process_identity(os.getpid()),
            }
            checkpoint.write_text(json.dumps(record), encoding="utf-8")
            os.chmod(checkpoint, 0o600)
            _copy_source(Path(cwd), stage)
            args = [
                "run", "--rm", "--init", "--pull=never", "--name", name,
                "--log-driver", "none",
                "--label", "poo-ia.validation=1",
                "--network", "none", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges=true", "--read-only",
                "--memory", factory.memory, "--memory-swap", factory.memory,
                "--cpus", str(factory.cpus), "--pids-limit", str(factory.pids_limit),
                "--ulimit", "nofile=1024:1024", "--stop-timeout", "5",
                "--user", f"{os.getuid()}:{os.getgid()}",
                "--tmpfs", "/tmp:rw,nosuid,nodev,size=268435456,mode=1777",
                "--tmpfs", (
                    f"/workspace:rw,nosuid,nodev,size=536870912,mode=0700,uid={os.getuid()},gid={os.getgid()}"
                ),
                "--mount", f"type=bind,src={stage.resolve()},dst=/source,readonly",
                "--workdir", "/workspace", "--entrypoint", "/usr/bin/timeout",
            ]
            for key, value in _SAFE_ENV.items():
                args.extend(("--env", f"{key}={value}"))
            args.extend((self.image, "--signal=TERM", "--kill-after=5s", f"{limit:g}s",
                         "/opt/venv/bin/python", "-c", _ENTRYPOINT, *argv))
            result = factory._docker(*args, timeout=limit + 15.0)
            if result.returncode in {124, 137}:
                raise CommandTimedOut("El contenedor de pruebas alcanzó su límite de tiempo o memoria.", result=result)
            if result.returncode in {125, 126, 127}:
                raise ValidationSandboxError("El contenedor o su comando de pruebas no están disponibles.")
            return result
        finally:
            if checkpoint.exists():
                try:
                    factory._remove_container(name)
                except (OSError, CommandTimedOut):
                    # On SIGKILL this finally cannot run. The internal timeout
                    # still stops tests; startup/cancellation consumes this record.
                    pass
                else:
                    checkpoint.unlink(missing_ok=True)
                    shutil.rmtree(stage, ignore_errors=True)
            else:
                shutil.rmtree(stage, ignore_errors=True)


def _copy_source(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_dir():
        raise ValidationSandboxError("El repositorio de pruebas no es una carpeta regular.")
    destination.mkdir(mode=0o700)
    total_bytes = 0
    for directory, names, files in os.walk(source, followlinks=False):
        relative = Path(directory).relative_to(source)
        for name in tuple(names):
            if _excluded(name):
                names.remove(name)
                continue
            path = Path(directory) / name
            if path.is_symlink():
                raise ValidationSandboxError("El repositorio contiene enlaces; revisa las rutas antes de probar.")
            (destination / relative / name).mkdir(mode=0o700)
        for name in files:
            if _excluded(name):
                continue
            path = Path(directory) / name
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise ValidationSandboxError("El repositorio contiene enlaces o archivos especiales no admitidos.")
            total_bytes += info.st_size
            if total_bytes > 256 * 1024 * 1024:
                raise ValidationSandboxError("El repositorio excede el límite de copia para las pruebas aisladas.")
            shutil.copyfile(path, destination / relative / name, follow_symlinks=False)
            os.chmod(destination / relative / name, 0o700 if info.st_mode & 0o111 else 0o600)


def _excluded(name: str) -> bool:
    lowered = name.casefold()
    return (
        lowered in _EXCLUDED
        or lowered.startswith(".env")
        or ".env." in lowered
        or lowered.endswith((".env", ".pem", ".key", ".p12", ".pfx", ".tfstate"))
        or lowered.split(".", 1)[0] in {"secrets", "credentials"}
    )
