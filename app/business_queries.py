"""Parse bounded business reads without granting a model database access.

The first capability counts planned tasks for one dealer and one day, across
all types, states and users. Recognized requests with unsupported constraints
return a clarification instead of silently dropping a filter.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import date


COUNT_PLANNED_TASKS = "count-planned-tasks"
_COUNT = re.compile(r"\b(?:cuantas?|cantidad|numero|total|conteo|contar|cuenta|cuentes|contabiliza|contabilizar|resume|resumen|resumir|resumas)\b")
_TASKS = re.compile(r"\b(?:tareas?|tasks?)\b")
_READ = re.compile(r"\b(?:consulta|consultar|muestra|mostrar|ver|lista|listar|revisa|revisar|dime)\b")
_DEALER_HINT = re.compile(r"\b(?:dealer|agencia|distribuidor|concesionario)\b")
_SOURCE_PREFIX = re.compile(r"^(?:usando|segun|con base en|basandote en) (?:la |el )?(?:documentacion|brain)[, :]*")
_DOCUMENTATION = re.compile(
    r"\b(?:como|donde|explica|explicame|funciona|funcionan|scheduler|bot)\b|\bque (?:es|son)\b"
)
_DOCUMENTATION_SOURCE = re.compile(r"\b(?:codigo|documentacion|repositorio|repo|implementacion|esquema|modelo|funcion|metodo|endpoint)\b")
_MUTATION_START = re.compile(
    r"^(?:por favor )?(?:(?:puedes|podrias|quiero|necesito) (?:que )?)?"
    r"(?:agrega|agregar|anade|cambia|cambiar|modifica|modificar|edita|editar|"
    r"elimina|eliminar|borra|borrar|crea|crear|actualiza|actualizar|implementa|implementar|publica)\b"
)
_MUTATION = re.compile(r"\b(?:agrega|agregar|anade|cambia|cambiar|modifica|modificar|edita|editar|elimina|eliminar|borra|borrar|crea|crear|actualiza|actualizar|implementa|implementar|publica|publicar)\b")
_DEALER = re.compile(
    r"\b(?:dealer|agencia|distribuidor|concesionario)\b\s*"
    r"(?:(?:con\s+)?(?:id|c[oó]digo)\b\s*)?[:=#]?\s*[`'\"]?"
    r"([A-Za-z0-9][A-Za-z0-9_-]{0,63})[`'\"]?(?=$|[\s.,;!?])",
    re.IGNORECASE,
)
_ISO_DAY = re.compile(r"(?<![\w-])\d{4}-\d{2}-\d{2}(?![\w-])")
_RELATIVE_DAY = re.compile(r"\b(?:hoy|manana|ayer)\b")
_UNSUPPORTED_DATE = re.compile(
    r"\b(?:pasado|anteayer|proxim[oa]|siguiente|lunes|martes|miercoles|jueves|viernes|sabado|domingo|"
    r"semana|mes|entre|desde|hasta|rango|horas?|horario|turno)\b|"
    r"\b(?:por|en|de) la (?:manana|tarde|noche)\b|\b\d{1,2}:\d{2}\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b"
)
_STATE_FILTER = re.compile(r"\b(?:estado|estatus|pendientes?|completad[oa]s?|cancelad[oa]s?|atrasad[oa]s?|activ[oa]s?|inactiv[oa]s?|abiert[oa]s?|cerrad[oa]s?|terminad[oa]s?|finalizad[oa]s?|hech[oa]s?|disponibles?)\b")
_USER_FILTER = re.compile(r"\b(?:usuarios?|asignad[oa]s?|responsables?|mi|mis|mias?|mios?|personales|empleados?|vendedor(?:es)?)\b")
_TYPES = re.compile(r"\b(?:asesor(?:a|es|as)?|tecnic[oa]s?|mantenimiento)\b")
_INCLUSIVE_TYPES = re.compile(
    r"\b(?:todos(?: los)? tipos|todo tipo|etc(?:etera)?|entre otros)\b|"
    r"\btodas(?: las)? tareas\b.*\b(?:incluyendo|incluye|considera)\b"
)
_LIMITED = re.compile(r"\b(?:solo|solamente|unicamente|exclusivamente|excepto|excluye|excluyendo|sin)\b")
_ALLOWED_WORDS = frozenset(
    "puedes puede podrias podria pueden podrian me por favor necesito quiero quisiera saber "
    "dime dame dar darme hacer haz un una el la los las de del al a para en este ese esta esa "
    "que cuantos cuantas cuanta cuanto cantidad numero total conteo contar cuenta cuentes "
    "contabiliza contabilizar resume resumen resumir resumas tareas tarea tasks task "
    "tengo tenemos tiene tienen hay son existen dia fecha planeadas planeados planeada "
    "programadas programados programada planificadas planificados agendadas previstas "
    "hoy manana ayer todas todos todo tipo tipos considera considerando incluyendo incluye "
    "asesor asesores asesora asesoras tecnico tecnicos tecnica tecnicas mantenimiento "
    "etc etcetera entre otros y o con en csv formato damelo devuelvemelo regresamelo exportalo "
    "lectura modo solo aws dynamodb dynamo db".split()
)

DEALER_CLARIFICATION = "Indica un único dealer para contar sus tareas planeadas, por ejemplo «cuántas tareas hay mañana en el dealer DEALER_ID»."
DAY_CLARIFICATION = "Indica un solo día: hoy, mañana, ayer o una fecha YYYY-MM-DD. El conteo disponible abarca el día completo."
FILTER_CLARIFICATION = "El conteo disponible incluye todas las tareas planeadas de un dealer en un día. Todavía no admite filtros adicionales; indica solo dealer y día."
TYPE_CLARIFICATION = "El conteo disponible incluye todos los tipos de tarea. Todavía no admite un filtro por tipo ni exclusiones de categorías."


@dataclass(frozen=True, slots=True)
class PlannedTaskCount:
    dealer_id: str
    day: str

    def to_payload(self, requested_at: float) -> dict[str, object]:
        if isinstance(requested_at, bool) or not isinstance(requested_at, (int, float)):
            raise ValueError("Business query reference time must be finite")
        try:
            timestamp = float(requested_at)
        except (ValueError, OverflowError) as error:
            raise ValueError("Business query reference time must be finite") from error
        if not math.isfinite(timestamp):
            raise ValueError("Business query reference time must be finite")
        return {"dealer_id": self.dealer_id, "day": self.day, "requested_at": timestamp}


@dataclass(frozen=True, slots=True)
class BusinessQueryDecision:
    query: PlannedTaskCount | None = None
    clarification: str | None = None


def _normalize(text: str) -> str:
    plain = "".join(character for character in unicodedata.normalize("NFKD", text.casefold()) if unicodedata.category(character) != "Mn")
    return " ".join(plain.split())


def parse_business_query(message: str) -> BusinessQueryDecision | None:
    """Return a validated read or clarification; ``None`` means another intent."""
    normalized = _SOURCE_PREFIX.sub("", _normalize(message))
    words = " ".join(re.findall(r"[a-z0-9]+", normalized))
    count_requested = bool(_COUNT.search(words))
    if not _TASKS.search(words) or not (count_requested or (_READ.search(words) and _DEALER_HINT.search(words))):
        return None
    if (_DOCUMENTATION.search(words) or _MUTATION_START.search(words)
            or (_DOCUMENTATION_SOURCE.search(words) and not _DEALER_HINT.search(words))):
        return None
    if _MUTATION.search(words):
        return BusinessQueryDecision(clarification="Esta consulta solo cuenta tareas planeadas. Separa cualquier modificación en una solicitud explícita de cambio.")
    if _STATE_FILTER.search(words):
        return BusinessQueryDecision(clarification="El conteo disponible incluye todos los estados; todavía no admite un filtro de estado de las tareas.")
    if _USER_FILTER.search(words):
        return BusinessQueryDecision(clarification="El conteo disponible abarca todo el dealer; todavía no admite un filtro por usuario o tareas personales.")
    if not count_requested:
        return BusinessQueryDecision(clarification="Por ahora puedo contar las tareas planeadas de un dealer para un día completo. Indica si necesitas ese total, con el dealer y el día.")
    if re.search(r"\b(?:cread[oa]s?|creacion|modificad[oa]s?|actualizad[oa]s?)\b", words):
        return BusinessQueryDecision(clarification="Puedo contar tareas planeadas para un día; todavía no admite filtros por fecha de creación o modificación.")
    without_read_only = re.sub(r"\bsolo lectura\b", "lectura", words)
    if _LIMITED.search(without_read_only) or (_TYPES.search(words) and not _INCLUSIVE_TYPES.search(words)):
        return BusinessQueryDecision(clarification=TYPE_CLARIFICATION)

    matches = list(_DEALER.finditer(message))
    identifiers = {match.group(1) for match in matches}
    reserved = _ALLOWED_WORDS | {"dealer", "agencia", "distribuidor", "concesionario", "id", "codigo"}
    if len(identifiers) != 1 or any(
        value.casefold() in reserved or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value)
        for value in identifiers
    ):
        return BusinessQueryDecision(clarification=DEALER_CLARIFICATION)
    dealer_id = next(iter(identifiers))
    remainder = _SOURCE_PREFIX.sub("", _normalize(_DEALER.sub(" ", message)))
    if _UNSUPPORTED_DATE.search(re.sub(r"\bentre otros\b", "", remainder)):
        return BusinessQueryDecision(clarification=DAY_CLARIFICATION)
    dates = set(_ISO_DAY.findall(remainder))
    relative_days = set(_RELATIVE_DAY.findall(remainder))
    if len(dates) + len(relative_days) != 1:
        return BusinessQueryDecision(clarification=DAY_CLARIFICATION)
    if dates:
        day = next(iter(dates))
        try:
            date.fromisoformat(day)
        except ValueError:
            return BusinessQueryDecision(clarification=DAY_CLARIFICATION)
    else:
        day = {"hoy": "today", "manana": "tomorrow", "ayer": "yesterday"}[next(iter(relative_days))]
    remaining_words = set(re.findall(r"[a-z0-9_]+", _ISO_DAY.sub(" ", remainder)))
    if remaining_words - _ALLOWED_WORDS:
        return BusinessQueryDecision(clarification=FILTER_CLARIFICATION)
    return BusinessQueryDecision(query=PlannedTaskCount(dealer_id=dealer_id, day=day))
