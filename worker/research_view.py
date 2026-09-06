"""Build an atomic, read-only research view from committed Git content.

OpenCode never receives the operational checkouts.  This exporter streams each
repository's ``HEAD`` archive, copies only ordinary text files, excludes common
credential material, and omits all Git metadata and untracked files.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import re
import signal
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
from pathlib import Path, PurePosixPath


MAX_FILE_BYTES = 2 * 1024 * 1024
ARCHIVE_TIMEOUT_SECONDS = 60.0
VIEW_BUILD_TIMEOUT_SECONDS = 8 * 60.0
_REPOSITORY_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_TEXT_SUFFIXES = frozenset(
    {
        ".adoc", ".cfg", ".conf", ".cs", ".css", ".go",
        ".graphql", ".h", ".hpp", ".html", ".ini", ".java", ".js",
        ".json", ".jsonc", ".jsx", ".kt", ".md", ".mdx", ".php",
        ".proto", ".py", ".pyi", ".rb", ".rs", ".rst", ".scss",
        ".sh", ".sql", ".svelte", ".toml", ".ts", ".tsx", ".txt",
        ".vue", ".xml", ".yaml", ".yml",
    }
)
_TEXT_NAMES = frozenset(
    {
        "dockerfile", "makefile", "readme", "license", "notice",
        "changelog", "procfile", "gemfile", "rakefile",
    }
)
_SENSITIVE_NAMES = frozenset(
    {
        "auth.json", "credentials", "credentials.json", "kubeconfig",
        "known_hosts", "authorized_keys",
    }
)
_SENSITIVE_SUFFIXES = (
    ".key", ".pem", ".p12", ".pfx", ".jks", ".keystore", ".tfstate",
)
_SECRET_CONTENT = (
    re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    re.compile(rb"\bsk_(?:live|test)_[A-Za-z0-9]{8,}\b"),
    re.compile(rb"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(rb"\bglpat-[A-Za-z0-9_-]{16,}\b"),
    re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{16,}\b"),
    re.compile(rb"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(rb"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(rb"[A-Za-z][A-Za-z0-9+.-]*://[^\s/:@]+:[^\s/@]{1,}@"),
)
_SECRET_ASSIGNMENT = re.compile(
    rb"(?im)^\s*(?:(?:export\s+)?(?:const|let|var)\s+|export\s+)?"
    rb"[\"']?[A-Za-z0-9_.-]*"
    rb"(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|"
    rb"client[_-]?secret|private[_-]?key)[A-Za-z0-9_.-]*"
    rb"[\"']?\s*[=:]\s*[\"']?"
    rb"([^\s#\"'`${}<>{}\[\],;]{1,})"
)
_SECRET_TYPED_ASSIGNMENT = re.compile(
    rb"(?im)^\s*(?:(?:export\s+)?(?:const|let|var)\s+)[A-Za-z0-9_.-]*"
    rb"(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|"
    rb"client[_-]?secret|private[_-]?key)[A-Za-z0-9_.-]*"
    rb"\s*:[^=\n]{1,80}=\s*[\"']?([^\s#\"'`${}<>{}\[\],;]{1,})"
)
_SECRET_JSON_ASSIGNMENT = re.compile(
    rb"(?i)[\"'][A-Za-z0-9_.-]*(?:password|passwd|secret|token|api[_-]?key|"
    rb"access[_-]?key|client[_-]?secret|private[_-]?key)[A-Za-z0-9_.-]*"
    rb"[\"']\s*:\s*[\"']?([^\s#\"'`${}<>{}\[\],;]{1,})"
)
_SECRET_PROSE_ASSIGNMENT = re.compile(
    rb"(?im)^\s*(?:[-*]\s*)?(?:[A-Za-z0-9_.-]+\s+){0,3}"
    rb"(?:password|passwd|secret|token|api[ _-]?key|access[ _-]?key|"
    rb"client[ _-]?secret|private[ _-]?key)\s*[=:]\s*[\"']?"
    rb"([^\s#\"'`${}<>{}\[\],;]{1,})"
)
_SECRET_QUOTED_ASSIGNMENT = re.compile(
    rb"(?im)(?:^|[,{]\s*)(?:(?:export\s+)?(?:const|let|var)\s+|export\s+)?"
    rb"[\"']?[A-Za-z0-9_. -]{0,64}"
    rb"(?:password|passwd|secret|token|api[ _-]?key|access[ _-]?key|"
    rb"client[ _-]?secret|private[ _-]?key)[A-Za-z0-9_. -]{0,64}[\"']?"
    rb"\s*(?::[^=\n]{1,80})?[=:]\s*(?P<quote>[\"'])"
    rb"(?P<value>[^\r\n]{1,1024}?)(?P=quote)"
)
_SECRET_KEYWORD_ASSIGNMENT = re.compile(
    rb"(?im)(?:^|[(,])\s*[A-Za-z0-9_.-]*"
    rb"(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|"
    rb"client[_-]?secret|private[_-]?key)[A-Za-z0-9_.-]*"
    rb"\s*=\s*[\"']?([^\s#\"'`${}<>{}\[\],;)]{1,})"
)
_SECRET_KEYWORD_QUOTED_ASSIGNMENT = re.compile(
    rb"(?im)(?:^|[(,])\s*[A-Za-z0-9_.-]*"
    rb"(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|"
    rb"client[_-]?secret|private[_-]?key)[A-Za-z0-9_.-]*"
    rb"\s*=\s*(?P<quote>[\"'])(?P<value>[^\r\n]{1,1024}?)(?P=quote)"
)
_SECRET_SUBSCRIPT_ASSIGNMENT = re.compile(
    rb"(?i)\[\s*[\"'][A-Za-z0-9_.-]*"
    rb"(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|"
    rb"client[_-]?secret|private[_-]?key)[A-Za-z0-9_.-]*[\"']\s*\]"
    rb"\s*=\s*[\"']?([^\s#\"'`${}<>{}\[\],;]{1,})"
)
_SECRET_SUBSCRIPT_QUOTED_ASSIGNMENT = re.compile(
    rb"(?i)\[\s*[\"'][A-Za-z0-9_.-]*"
    rb"(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|"
    rb"client[_-]?secret|private[_-]?key)[A-Za-z0-9_.-]*[\"']\s*\]"
    rb"\s*=\s*(?P<quote>[\"'])(?P<value>[^\r\n]{1,1024}?)(?P=quote)"
)
_PLACEHOLDER_VALUES = frozenset(
    {
        "0", "changeme", "disabled", "example", "false", "none", "null",
        "placeholder", "redacted", "replace-me", "secret", "token", "true",
        "undefined", "your-token",
    }
)
_SERVICE_SOURCE_SUFFIXES = frozenset(
    {
        ".adoc", ".cs", ".css", ".go", ".graphql", ".h", ".hpp",
        ".html", ".java", ".js", ".jsx", ".kt", ".md", ".mdx", ".php",
        ".proto", ".py", ".pyi", ".rb", ".rs", ".rst", ".scss", ".sh",
        ".sql", ".svelte", ".ts", ".tsx", ".txt", ".vue", ".xml",
    }
)


class ResearchViewError(RuntimeError):
    """The committed documentation view could not be built safely."""


def _safe_member_name(raw_name: str) -> PurePosixPath | None:
    path = PurePosixPath(raw_name)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        return None
    lowered = tuple(part.casefold() for part in path.parts)
    if any(part.startswith(".") for part in lowered):
        return None
    filename = lowered[-1]
    if filename.startswith(".env") or filename in _SENSITIVE_NAMES:
        return None
    if filename.startswith("id_") or filename.endswith(_SENSITIVE_SUFFIXES):
        return None
    suffix = PurePosixPath(filename).suffix
    stem_name = filename.split(".", 1)[0]
    if suffix not in _TEXT_SUFFIXES and stem_name not in _TEXT_NAMES:
        return None
    return path


def _safe_text(data: bytes) -> bool:
    if b"\0" in data:
        return False
    if any(pattern.search(data) for pattern in _SECRET_CONTENT):
        return False
    for pattern in (
        _SECRET_QUOTED_ASSIGNMENT,
        _SECRET_KEYWORD_QUOTED_ASSIGNMENT,
        _SECRET_SUBSCRIPT_QUOTED_ASSIGNMENT,
    ):
        for match in pattern.finditer(data):
            if not _placeholder_secret_value(match.group("value")):
                return False
    for pattern in (
        _SECRET_ASSIGNMENT,
        _SECRET_TYPED_ASSIGNMENT,
        _SECRET_JSON_ASSIGNMENT,
        _SECRET_PROSE_ASSIGNMENT,
        _SECRET_KEYWORD_ASSIGNMENT,
        _SECRET_SUBSCRIPT_ASSIGNMENT,
    ):
        for match in pattern.finditer(data):
            if not _placeholder_secret_value(match.group(1)):
                return False
    return True


def _placeholder_secret_value(raw: bytes) -> bool:
    candidate = raw.decode("utf-8", errors="ignore").strip().casefold()
    normalized = re.sub(r"[\s_-]+", "-", candidate)
    if normalized in _PLACEHOLDER_VALUES:
        return True
    if candidate and set(candidate) <= {"x", "*", "-", "_"}:
        return True
    if re.fullmatch(r"\$\{[a-z_][a-z0-9_]*\}", candidate):
        return True
    if candidate.startswith("<") and candidate.endswith(">"):
        inner = re.sub(r"[\s_-]+", "-", candidate[1:-1].strip())
        return inner in {
            "password", "secret", "token", "your-password", "your-secret",
            "your-token",
        }
    return False


def _brain_path_is_searchable(path: PurePosixPath) -> bool:
    """Enforce brain-capnet's versioned access policy before model retrieval."""
    if path == PurePosixPath("Inicio.md"):
        return True
    if not path.parts:
        return False
    first = path.parts[0]
    if first == "ai":
        return True
    if first in {"Servicios", "Decisiones", "Ideas", "Changelog"}:
        return path.suffix.casefold() in {".md", ".mdx"}
    if path.parts[:2] == ("Procesos", "Poo-IA") and len(path.parts) == 3:
        return path.suffix.casefold() == ".md"
    return path in {
        PurePosixPath("Arquitectura/Mapa-de-servicios.md"),
        PurePosixPath("Arquitectura/dependencias.json"),
        PurePosixPath("Arquitectura/Estado-de-despliegue.md"),
        PurePosixPath("Arquitectura/Ciclo-de-vida-de-tareas.md"),
    }


def _service_path_is_searchable(path: PurePosixPath) -> bool:
    """Expose implementation text, never repository config/data inventories."""
    filename = path.name.casefold()
    if filename.split(".", 1)[0] in {"config", "settings", "secret", "secrets", "credentials"}:
        return False
    return path.suffix.casefold() in _SERVICE_SOURCE_SUFFIXES or filename in _TEXT_NAMES


def _git_text(
    repository: Path,
    *arguments: str,
    timeout_seconds: float = ARCHIVE_TIMEOUT_SECONDS,
) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as error:
        raise ResearchViewError(
            f"git {' '.join(arguments)} timed out for {repository.name}"
        ) from error
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()[:300]
        raise ResearchViewError(
            f"git {' '.join(arguments)} failed for {repository.name}: {detail}"
        )
    return result.stdout.decode("utf-8", errors="strict").strip()


def _direct_repository_children(source: Path) -> tuple[Path, ...]:
    repositories: list[Path] = []
    for child in sorted(source.iterdir(), key=lambda item: item.name.casefold()):
        if child.is_symlink() or not _REPOSITORY_NAME.fullmatch(child.name):
            continue
        try:
            resolved = child.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        git_directory = resolved / ".git"
        if (
            resolved.parent != source
            or not resolved.is_dir()
            or git_directory.is_symlink()
            or not git_directory.is_dir()
        ):
            continue
        repositories.append(resolved)
    return tuple(repositories)


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return


def _export_repository(
    repository: Path,
    destination: Path,
    *,
    timeout_seconds: float = ARCHIVE_TIMEOUT_SECONDS,
) -> tuple[str, int, int]:
    if timeout_seconds <= 0:
        raise ResearchViewError(f"git archive timed out for {repository.name}")
    started_at = time.monotonic()
    commit = _git_text(
        repository,
        "rev-parse",
        "--verify",
        "HEAD",
        timeout_seconds=timeout_seconds,
    )
    remaining = timeout_seconds - (time.monotonic() - started_at)
    if remaining <= 0:
        raise ResearchViewError(f"git archive timed out for {repository.name}")
    process = subprocess.Popen(
        ["git", "-C", str(repository), "archive", "--format=tar", commit],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stdout is not None and process.stderr is not None
    timed_out = threading.Event()

    def expire() -> None:
        timed_out.set()
        _terminate_process_group(process)

    timer = threading.Timer(remaining, expire)
    timer.daemon = True
    timer.start()
    included = 0
    skipped = 0
    try:
        with process.stdout as archive_stream, process.stderr as error_stream:
            with tarfile.open(fileobj=archive_stream, mode="r|*") as archive:
                for member in archive:
                    safe_path = _safe_member_name(member.name)
                    if (
                        safe_path is None
                        or (
                            repository.name == "brain-capnet"
                            and not _brain_path_is_searchable(safe_path)
                        )
                        or (
                            repository.name != "brain-capnet"
                            and not _service_path_is_searchable(safe_path)
                        )
                        or not member.isfile()
                        or member.size < 0
                        or member.size > MAX_FILE_BYTES
                    ):
                        skipped += 1
                        continue
                    source = archive.extractfile(member)
                    if source is None:
                        skipped += 1
                        continue
                    data = source.read(MAX_FILE_BYTES + 1)
                    if len(data) != member.size or not _safe_text(data):
                        skipped += 1
                        continue
                    output = destination.joinpath(*safe_path.parts)
                    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    output.write_bytes(data)
                    output.chmod(0o600)
                    included += 1
            stderr = error_stream.read().decode("utf-8", errors="replace").strip()
    except Exception as error:
        _terminate_process_group(process)
        process.wait(timeout=10)
        if timed_out.is_set():
            raise ResearchViewError(
                f"git archive timed out for {repository.name}"
            ) from error
        raise
    finally:
        timer.cancel()
    returncode = process.wait(timeout=10)
    if timed_out.is_set():
        raise ResearchViewError(f"git archive timed out for {repository.name}")
    if returncode != 0:
        raise ResearchViewError(
            f"git archive failed for {repository.name}: {stderr[:300]}"
        )
    return commit, included, skipped


def build_research_view(source_root: Path, target_root: Path) -> tuple[str, ...]:
    """Replace ``target_root`` with a committed, sanitized multi-repo snapshot."""
    source = source_root.resolve(strict=True)
    requested_target = target_root.absolute()
    requested_target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = requested_target.parent.resolve(strict=True) / requested_target.name
    if source == target or source in target.parents or target in source.parents:
        raise ResearchViewError("source and target roots must be disjoint")
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise ResearchViewError("target must be a real directory, never a symlink")
    lock_path = target.parent / f".{target.name}.lock"
    backup = target.parent / f".{target.name}.previous"
    if lock_path.is_symlink() or backup.is_symlink():
        raise ResearchViewError("research-view control paths must not be symlinks")
    if backup.exists() and not backup.is_dir():
        raise ResearchViewError("research-view backup path must be a directory")
    repositories = _direct_repository_children(source)
    if not repositories or not any(repo.name == "brain-capnet" for repo in repositories):
        raise ResearchViewError("brain-capnet and at least one Git repository are required")

    with lock_path.open("a+b") as lock_file:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
        try:
            staging.chmod(0o700)
            manifest_lines = [
                "# Poo-IA research view",
                "",
                "Generated from committed Git HEAD trees; no .git or untracked files.",
                "",
            ]
            exported: list[str] = []
            deadline = time.monotonic() + VIEW_BUILD_TIMEOUT_SECONDS
            for repository in repositories:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ResearchViewError("research view build timed out")
                repository_target = staging / repository.name
                repository_target.mkdir(mode=0o700)
                commit, included, skipped = _export_repository(
                    repository,
                    repository_target,
                    timeout_seconds=min(ARCHIVE_TIMEOUT_SECONDS, remaining),
                )
                manifest_lines.append(
                    f"- {repository.name}: `{commit}` ({included} included, {skipped} skipped)"
                )
                exported.append(repository.name)
            manifest = staging / "VIEW-MANIFEST.md"
            manifest.write_text("\n".join(manifest_lines) + "\n", encoding="utf-8")
            manifest.chmod(0o600)

            if backup.exists():
                shutil.rmtree(backup)
            if target.exists():
                os.replace(target, backup)
            os.replace(staging, target)
            if backup.exists():
                shutil.rmtree(backup)
            return tuple(exported)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            if not target.exists() and backup.exists():
                os.replace(backup, target)
            raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    arguments = parser.parse_args()
    exported = build_research_view(arguments.source, arguments.target)
    print(f"research view ready: {len(exported)} repositories")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
