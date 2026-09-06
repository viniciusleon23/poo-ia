"""Deterministic intent routing for the Poo-IA core.

The router deliberately does not ask a model whether a message authorises a
mutation. A model may help research a request, but only explicit patterns in
this module can select the Codex/GitHub worker.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

from .models import Backend, Intent, RouteDecision
from .repository_scope import (
    AMBIGUOUS_REPOSITORY,
    DOCUMENTATION_REPOSITORY_READ_ONLY,
    REPOSITORY_REQUIRED,
    UNKNOWN_REPOSITORY,
    is_documentation_repository,
    resolve_execution_repository,
)


AWS_DISABLED = "AWS_DISABLED"


SMALL_TALK = frozenset(
    {
        "adios",
        "buen dia",
        "buenas",
        "buenas noches",
        "buenas tardes",
        "buenos dias",
        "como estas",
        "gracias",
        "hasta luego",
        "hey",
        "hola",
        "hola mundo",
        "muchas gracias",
        "que tal",
    }
)

FORGET_PHRASES = frozenset({"olvida la conversacion"})
CAPABILITY_PHRASES = frozenset(
    {"ya puedes editar", "puedes editar", "que puedes hacer", "puedes hacer cambios"}
)
STATUS_PHRASES = frozenset(
    {
        "como va",
        "como va el trabajo",
        "como va la tarea",
        "estado",
        "estado del trabajo",
        "estado de la tarea",
        "que estado tiene el trabajo",
    }
)

_AWS_PATTERN = re.compile(r"\b(?:aws|dynamo\s*db|dynamodb|amazon web services)\b")
_PR_PATTERN = re.compile(
    r"^(?:por favor\s+)?(?:"
    r"(?:arma|abre|crea|publica|sube|haz)\s+(?:el\s+|un\s+)?(?:pr|pull request)|"
    r"(?:quiero|necesito)\s+(?:que\s+)?(?:armes|abras|crees|publiques|subas|"
    r"armar|abrir|crear|publicar|subir)\s+(?:el\s+|un\s+)?(?:pr|pull request)|"
    r"(?:puedes|podrias)\s+(?:armar|abrir|crear|publicar|subir)\s+"
    r"(?:el\s+|un\s+)?(?:pr|pull request)"
    r")\b|"
    r"\b(?:y|ademas)\s+(?:arma|abre|crea|publica|sube)\s+"
    r"(?:el\s+|un\s+)?(?:pr|pull request)\b"
)
_CANCEL_PATTERN = re.compile(
    r"^(?:por favor )?(?:(?:cancela|cancelar|deten|detener|aborta|abortar)\b|"
    r"(?:para|parar)\s+(?:el|ese|este|mi)\s+(?:trabajo|job|tarea)\b)"
)
_STATUS_PATTERN = re.compile(
    r"^(?:como va (?:el |ese |este |mi )?(?:trabajo|job|tarea)|"
    r"dime (?:el )?estado|consulta (?:el )?estado|"
    r"muestra (?:el )?estado|estado de|estado del)\b"
)
_CODE_CHANGE_PATTERN = re.compile(
    r"^(?:por favor )?(?:(?:hecho|ok|okay|vale|perfecto|si)\s+)*(?:"
    r"agrega|anade|cambia|modifica|edita|editar|edit|elimina|borra|implementa|"
    r"corrige|arregla|crea|actualiza|renombra|reemplaza|aplica|haz el cambio|"
    r"hazlo|agregalo|anadelo|cambialo|modificalo|editalo|eliminalo|borralo|"
    r"implementalo|corrigelo|arreglalo|actualizalo|renombralo|reemplazalo|"
    r"aplicalo)\b|"
    r"\b(?:quiero|necesito|puedes|podrias|debes|hay que|vamos a|te pido)\s+"
    r"(?:que\s+)?(?:agregar|anadir|cambiar|modificar|editar|eliminar|borrar|"
    r"implementar|corregir|arreglar|crear|actualizar|renombrar|reemplazar|"
    r"aplicar|agregues|anadas|cambies|modifiques|edites|elimines|implementes|"
    r"corrijas|arregles|crees|actualices|renombres|reemplaces|apliques)\b|"
    r"^(?:por favor )?(?:lee|consulta|revisa|busca)\b.*\b(?:y|luego)\s+"
    r"(?:agrega|anade|cambia|modifica|edita|elimina|"
    r"implementa|corrige|arregla|actualiza|aplica)\b"
)
_FOLLOW_UP_CHANGE = frozenset(
    {
        "adelante",
        "agregalo",
        "anadelo",
        "aprobado",
        "apruebo",
        "arreglalo",
        "borralo",
        "cambialo",
        "corrigelo",
        "dale",
        "eliminalo",
        "editalo",
        "hazlo",
        "implementalo",
        "modificalo",
        "aplicalo",
        "actualizalo",
        "renombralo",
        "reemplazalo",
    }
)
_UUID_PATTERN = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_NAMED_JOB_PATTERN = re.compile(
    r"\b(?:job|trabajo|tarea)\s+#?([a-z0-9][a-z0-9_-]{5,63})\b",
    re.IGNORECASE,
)


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    without_accents = "".join(
        character for character in decomposed if unicodedata.category(character) != "Mn"
    )
    return re.sub(r"[^a-z0-9._-]+", " ", without_accents).strip()


def _phrase_normalize(text: str) -> str:
    """Normalize punctuation, accents and repository separators for prose rules."""
    return re.sub(r"[^a-z0-9]+", " ", _normalize(text)).strip()


def _extract_job_id(message: str, active_job_id: str | None) -> str | None:
    uuid_match = _UUID_PATTERN.search(message)
    if uuid_match:
        return uuid_match.group(0)

    named_match = _NAMED_JOB_PATTERN.search(message)
    if named_match:
        candidate = named_match.group(1)
        # Avoid treating ordinary phrases such as "trabajo actual" as an ID.
        if candidate.casefold() not in {"actual", "activo", "anterior", "pendiente"}:
            return candidate
    return active_job_id


def classify_intent(message: str, *, has_active_change: bool = False) -> Intent:
    """Classify explicit controls/mutations and default technical text to research."""
    normalized = _phrase_normalize(message)
    if normalized in FORGET_PHRASES:
        return Intent.FORGET
    if normalized in CAPABILITY_PHRASES:
        return Intent.CAPABILITIES
    if normalized in STATUS_PHRASES or _STATUS_PATTERN.search(normalized):
        return Intent.JOB_STATUS
    if _CANCEL_PATTERN.search(normalized):
        return Intent.CANCEL
    if _AWS_PATTERN.search(normalized):
        return Intent.AWS_REPORT
    if _PR_PATTERN.search(normalized):
        return Intent.PULL_REQUEST
    if _CODE_CHANGE_PATTERN.search(normalized):
        return Intent.CODE_CHANGE
    if has_active_change and normalized in _FOLLOW_UP_CHANGE:
        return Intent.CODE_CHANGE
    if normalized in SMALL_TALK:
        return Intent.CHAT
    return Intent.RESEARCH


def explicitly_requests_code_change(message: str) -> bool:
    """Detect a mutation verb even when the same message also requests a PR."""
    return bool(_CODE_CHANGE_PATTERN.search(_phrase_normalize(message)))


def route_message(
    message: str,
    *,
    active_repository: str | None = None,
    active_job_id: str | None = None,
    repositories: Iterable[str] = (),
) -> RouteDecision:
    """Return a transport-neutral deterministic route for one owner message."""
    intent = classify_intent(
        message,
        has_active_change=(
            active_repository is not None and not is_documentation_repository(active_repository)
        ) or active_job_id is not None,
    )

    if intent is Intent.CHAT:
        return RouteDecision(intent, Backend.OLLAMA)
    if intent is Intent.RESEARCH:
        repository, _ = resolve_execution_repository(message, active_repository, repositories)
        return RouteDecision(intent, Backend.OPENCODE, repository=repository)
    if intent is Intent.AWS_REPORT:
        return RouteDecision(intent, Backend.NONE, reason=AWS_DISABLED)
    if intent in {Intent.FORGET, Intent.CAPABILITIES}:
        return RouteDecision(intent, Backend.STORAGE)
    if intent in {Intent.JOB_STATUS, Intent.CANCEL}:
        return RouteDecision(
            intent,
            Backend.STORAGE,
            job_id=_extract_job_id(message, active_job_id),
        )

    job_id = _extract_job_id(message, active_job_id)
    repository, repository_error = resolve_execution_repository(
        message,
        active_repository=active_repository,
        repositories=repositories,
    )
    if intent is Intent.PULL_REQUEST and job_id is not None and repository_error in {
        None, REPOSITORY_REQUIRED
    }:
        return RouteDecision(intent, Backend.WORKER, repository=repository, job_id=job_id)
    if repository_error is not None:
        return RouteDecision(
            Intent.CLARIFY,
            Backend.NONE,
            job_id=job_id,
            reason=repository_error,
        )
    return RouteDecision(
        intent,
        Backend.WORKER,
        repository=repository,
        job_id=job_id,
    )


def choose_backend(message: str) -> Backend:
    """Preserve the original two-backend API used by the current Discord adapter."""
    return Backend.OLLAMA if _phrase_normalize(message) in SMALL_TALK else Backend.OPENCODE
