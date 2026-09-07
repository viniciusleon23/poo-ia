"""Deterministic separation of documentation sources and execution repositories.

Reading the brain never establishes a repository for code changes. Aliases only
resolve to repositories present in the configured inventory, and explicit new
references always take precedence over remembered execution context.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable


BRAIN_REPOSITORY = "brain-capnet"
REPOSITORY_REQUIRED = "REPOSITORY_REQUIRED"
AMBIGUOUS_REPOSITORY = "AMBIGUOUS_REPOSITORY"
UNKNOWN_REPOSITORY = "UNKNOWN_REPOSITORY"
DOCUMENTATION_REPOSITORY_READ_ONLY = "DOCUMENTATION_REPOSITORY_READ_ONLY"

_DOCUMENTATION_NAMES = frozenset({BRAIN_REPOSITORY, "capnet-brain"})
_EXECUTION_ALIASES = {
    "capnet-next-lambda-tasks": ("task", "tasks", "tarea", "tareas"),
    "capnet-next-lambda-task-manager": ("task manager",),
}
_EXPLICIT_REPOSITORY_PATTERN = re.compile(
    r"\b(?:repo|repositorio)\s+(?:(?:llamado|de nombre)\s+)?"
    r"[`'\"]?([a-z0-9][a-z0-9._/-]*)"
)
_REPOSITORY_PROSE = frozenset(
    {
        "a", "al", "de", "del", "donde", "el", "la", "los", "las", "en",
        "que", "y", "o", "para", "con", "sin", "ese", "este", "aquel",
        "mismo", "actual", "activo", "anterior", "elegido", "indicado",
        "correcto", "destino", "correspondiente",
    }
)
_CHANGE_VERB_PATTERN = re.compile(
    r"\b(?:agrega|agregar|anade|anadir|edita|editar|edit|modifica|modificar|"
    r"cambia|cambiar|actualiza|actualizar|borra|borrar|elimina|eliminar|"
    r"implementa|implementar|corrige|corregir|arregla|arreglar|crea|crear|"
    r"abre|abrir|publica|publicar)\b"
)
_DOCUMENTATION_SOURCE_PATTERN = re.compile(
    r"\b(?:lee|leer|consulta|consultar|revisa|revisar|documentacion|"
    r"referencia|segun|siguiendo|basado en)\b"
)


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(
        character for character in decomposed if unicodedata.category(character) != "Mn"
    ).strip()


def is_documentation_repository(name: str | None) -> bool:
    return bool(name and _normalize(name) in _DOCUMENTATION_NAMES)


def execution_repositories(repositories: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for repository in repositories:
        repository = str(repository).strip()
        normalized = _normalize(repository)
        if normalized and normalized not in seen and not is_documentation_repository(repository):
            result.append(repository)
            seen.add(normalized)
    return tuple(result)


def _name_pattern(name: str) -> re.Pattern[str]:
    # Keep identifier boundaries: task_available, feature/tasks and tasks.py
    # must not accidentally select the tasks repository. A final period is prose.
    phrase = re.escape(_normalize(name)).replace(r"\ ", r"\s+")
    return re.compile(r"(?<![a-z0-9._/-])" + phrase + r"(?![a-z0-9_/-]|\.[a-z0-9_])")


def _matching_names(text: str, names: Iterable[tuple[str, str]]) -> tuple[str, ...]:
    matches = [
        (match.start(), match.end(), repository)
        for name, repository in names
        for match in _name_pattern(name).finditer(text)
    ]
    # Prefer a longer phrase only when it contains the shorter match. Independent
    # repository references remain ambiguous even when their names differ in size.
    selected = [
        (start, repository)
        for start, end, repository in matches
        if not any(
            other_start <= start and end <= other_end and other_end - other_start > end - start
            for other_start, other_end, _ in matches
        )
    ]
    return tuple(dict.fromkeys(repository for _, repository in sorted(selected)))


def mentioned_execution_repositories(
    text: str, repositories: Iterable[str]
) -> tuple[str, ...]:
    available = execution_repositories(repositories)
    normalized = _normalize(text)
    full_names = _matching_names(normalized, ((name, name) for name in available))
    if full_names:
        return full_names
    aliases = (
        (alias, repository)
        for repository in available
        for alias in _EXECUTION_ALIASES.get(_normalize(repository), ())
    )
    return _matching_names(normalized, aliases)


def _has_unknown_explicit_repository(text: str, available: tuple[str, ...]) -> bool:
    known = {_normalize(repository) for repository in available} | _DOCUMENTATION_NAMES
    for repository in available:
        known.update(alias.split()[0] for alias in _EXECUTION_ALIASES.get(_normalize(repository), ()))
    return any(
        candidate not in known and candidate not in _REPOSITORY_PROSE
        for match in _EXPLICIT_REPOSITORY_PATTERN.finditer(text)
        if (candidate := match.group(1).rstrip("."))
    )


def _requests_documentation_mutation(text: str) -> bool:
    for name in _DOCUMENTATION_NAMES:
        for mention in _name_pattern(name).finditer(text):
            # A read clause such as "lee brain-capnet y agrega ... en tasks"
            # does not authorize a mutation of the brain. A write clause does
            # not become safe simply because it also names an execution repo.
            clause = re.split(r"[;,\n]|\b(?:y|luego)\b", text[:mention.start()])[-1]
            if _CHANGE_VERB_PATTERN.search(clause) and not _DOCUMENTATION_SOURCE_PATTERN.search(clause):
                return True
    return False


def resolve_execution_repository(
    text: str,
    active_repository: str | None,
    repositories: Iterable[str],
) -> tuple[str | None, str | None]:
    available = execution_repositories(repositories)
    normalized = _normalize(text)
    if _has_unknown_explicit_repository(normalized, available):
        return None, UNKNOWN_REPOSITORY
    if _requests_documentation_mutation(normalized):
        return None, DOCUMENTATION_REPOSITORY_READ_ONLY

    matches = mentioned_execution_repositories(text, available)
    if len(matches) > 1:
        return None, AMBIGUOUS_REPOSITORY
    if matches:
        return matches[0], None
    if any(_name_pattern(name).search(normalized) for name in _DOCUMENTATION_NAMES):
        return None, DOCUMENTATION_REPOSITORY_READ_ONLY

    if active_repository and not is_documentation_repository(active_repository):
        if not available:
            return active_repository, None
        for repository in available:
            if _normalize(repository) == _normalize(active_repository):
                return repository, None
    return None, REPOSITORY_REQUIRED
