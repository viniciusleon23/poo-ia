"""Fixed, paginated task counts using the Tasks planning date contract.

This module does not import application repositories or load their credentials.
Only the configured DynamoDB tables and fixed indexes can be queried. Provider
records and pagination keys remain inside this worker; reports contain aggregates.
"""

from __future__ import annotations

import base64
import json
import math
import re
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, time as day_time, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .aws_csv import build_csv_attachment
from .config import WorkerSettings
from .processes import CommandRunner, CommandTimedOut


ACTION = "count-planned-tasks"
PAGE_LIMIT = 500
MAX_PAGES = 100
MAX_EVALUATED = 50_000
MAX_SECONDS = 20.0
MAX_CAPTURE_BYTES = 65_536
_DEALER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_TABLE_NAME = re.compile(r"[A-Za-z0-9_.-]{3,255}\Z")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_ZONE_NAME = re.compile(r"[A-Za-z0-9._+-]+(?:/[A-Za-z0-9._+-]+)*\Z")


class _QueryFailure(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _encoded(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _request(query: object) -> tuple[str, str, float]:
    if not isinstance(query, dict) or set(query) != {"dealer_id", "day", "requested_at"}:
        raise ValueError("La consulta de tareas requiere dealer_id, day y requested_at, sin campos adicionales.")
    dealer_id, day, requested_at = query["dealer_id"], query["day"], query["requested_at"]
    if not isinstance(dealer_id, str) or not _DEALER_ID.fullmatch(dealer_id):
        raise ValueError("Indica un identificador válido de dealer.")
    if not isinstance(day, str) or day not in {"today", "tomorrow", "yesterday"} and not _DATE.fullmatch(day):
        raise ValueError("Indica today, tomorrow, yesterday o una fecha YYYY-MM-DD válida.")
    if day not in {"today", "tomorrow", "yesterday"}:
        try:
            date.fromisoformat(day)
        except ValueError as error:
            raise ValueError("La fecha de consulta no es válida.") from error
    if isinstance(requested_at, bool) or not isinstance(requested_at, (int, float)):
        raise ValueError("La fecha de recepción de la consulta no es válida.")
    try:
        requested_at = float(requested_at)
        if not math.isfinite(requested_at):
            raise ValueError
        datetime.fromtimestamp(requested_at, UTC)
    except (OverflowError, OSError, ValueError) as error:
        raise ValueError("La fecha de recepción de la consulta no es válida.") from error
    return dealer_id, day, float(requested_at)


def planning_bounds(day: str, requested_at: float, zone: ZoneInfo) -> tuple[date, str, str]:
    """Match Tasks /planning, including its mixed UTC timestamp representations."""
    if day in {"today", "tomorrow", "yesterday"}:
        offset = {"today": 0, "tomorrow": 1, "yesterday": -1}[day]
        planned_day = datetime.fromtimestamp(requested_at, zone).date() + timedelta(days=offset)
    else:
        planned_day = date.fromisoformat(day)
    start = datetime.combine(planned_day, day_time.min, zone).astimezone(UTC)
    end = datetime.combine(planned_day + timedelta(days=1), day_time.min, zone).astimezone(UTC)
    # The lower bare timestamp also includes a stored +00:00/Z suffix. The final
    # second ending in Z sorts after legacy fractional seconds and +00:00 forms.
    lower = start.replace(tzinfo=None).isoformat(timespec="seconds")
    upper = (end - timedelta(seconds=1)).replace(tzinfo=None).isoformat(timespec="seconds") + "Z"
    return planned_day, lower, upper


def _count(value: object, limit: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= limit:
        raise _QueryFailure("invalid-output", "AWS devolvió un conteo con formato inválido.")
    return value


def _cursor(value: object) -> dict[str, object] | None:
    if value is None or value == {}:
        return None
    if not isinstance(value, dict) or not 1 <= len(value) <= 8:
        raise _QueryFailure("invalid-output", "AWS devolvió una clave de paginación inválida.")
    for name, attribute in value.items():
        if not isinstance(name, str) or not 1 <= len(name) <= 255 or not isinstance(attribute, dict) or len(attribute) != 1:
            raise _QueryFailure("invalid-output", "AWS devolvió una clave de paginación inválida.")
        kind, data = next(iter(attribute.items()))
        if kind not in {"S", "N", "B"} or not isinstance(data, str) or not 1 <= len(data.encode("utf-8")) <= 4096:
            raise _QueryFailure("invalid-output", "AWS devolvió una clave de paginación inválida.")
        try:
            if kind == "N" and (len(data) > 128 or not Decimal(data).is_finite()):
                raise ValueError
            if kind == "B":
                base64.b64decode(data, validate=True)
        except (InvalidOperation, ValueError) as error:
            raise _QueryFailure("invalid-output", "AWS devolvió una clave de paginación inválida.") from error
    if len(_encoded(value)) > 16_384:
        raise _QueryFailure("invalid-output", "AWS devolvió una clave de paginación demasiado grande.")
    return value


class BusinessQueries:
    def __init__(
        self,
        settings: WorkerSettings,
        *,
        runner: CommandRunner,
        provider_error: Callable[[str], dict[str, object]],
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.runner = runner
        self.provider_error = provider_error
        self.monotonic = monotonic

    def count_planned_tasks(self, query: object, *, output_format: str) -> dict[str, object]:
        dealer_id, day, requested_at = _request(query)
        tasks_table = self.settings.aws_tasks_table
        config_table = self.settings.aws_dealer_config_table
        if any(not isinstance(table, str) or not _TABLE_NAME.fullmatch(table) for table in (tasks_table, config_table)):
            return self._failure("configuration", "Falta configurar las tablas de Tasks y DealerConfig del entorno en el worker; no se consultó otro entorno.")
        deadline = self.monotonic() + min(self.settings.aws_query_timeout_seconds, MAX_SECONDS)
        try:
            zone = self._dealer_zone(config_table, dealer_id, deadline)
            try:
                planned_day, lower, upper = planning_bounds(day, requested_at, zone)
            except (OverflowError, ValueError, OSError) as error:
                raise _QueryFailure("invalid-date", "La fecha no permite construir el intervalo de planeación del dealer.") from error
            total, pages = self._task_count(tasks_table, dealer_id, lower, upper, deadline)
            self._remaining(deadline)
        except _QueryFailure as error:
            return self._failure(error.code, error.message)

        source = f"DynamoDB:{self.settings.aws_region}:{tasks_table}/dealer_id-planned_start_at-index"
        summary: dict[str, object] = {
            "dealer_id": dealer_id, "date": planned_day.isoformat(),
            "time_zone": zone.key, "count": total, "source": source,
        }
        message = (
            f"Dealer {dealer_id}: {total} tareas planeadas para {planned_day.isoformat()} "
            f"({zone.key}), de todos los tipos.\n"
            "Incluye todos los tipos y estados; excluye tareas sin inicio planeado.\n"
            f"Paginación completa: {pages} página(s). Lectura con consistencia eventual; puede no incluir cambios recientes.\n"
            f"Fuente: {tasks_table} en {self.settings.aws_region}."
        )
        report: dict[str, object] = {
            "state": "succeeded", "action": ACTION, "message": message,
            "complete": True, "count": total, "date": planned_day.isoformat(),
            "time_zone": zone.key, "pages": pages,
        }
        if output_format == "csv":
            export = build_csv_attachment(ACTION, {"Summary": summary}, row_limit=1, redact=lambda value: value)
            report["attachment"] = export.attachment
        return report

    @staticmethod
    def _failure(code: str, message: str) -> dict[str, object]:
        return {
            "state": "failed", "action": ACTION, "complete": False,
            "error_code": code, "message": f"No se confirmó el total de tareas. {message}",
        }

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self.monotonic()
        if remaining <= 0:
            raise _QueryFailure("incomplete-timeout", "La consulta quedó incompleta al alcanzar el límite total de tiempo.")
        return remaining

    def _query_page(self, table: str, arguments: tuple[str, ...], deadline: float) -> dict[str, object]:
        remaining = self._remaining(deadline)
        argv = (
            self.settings.aws_executable,
            "--profile", self.settings.aws_profile, "--region", self.settings.aws_region,
            "--output", "json", "--no-cli-pager", "--no-cli-auto-prompt",
            "--cli-connect-timeout", "5", "--cli-read-timeout", "15",
            "dynamodb", "query", f"--table-name={table}",
            "--no-paginate", "--no-consistent-read", *arguments,
        )
        try:
            result = self.runner.run(argv, timeout=remaining, capture_limit_bytes=MAX_CAPTURE_BYTES)
        except CommandTimedOut as error:
            raise _QueryFailure("incomplete-timeout", "AWS no completó la consulta dentro del límite total de tiempo.") from error
        except OSError as error:
            raise _QueryFailure("unavailable", "AWS CLI no está disponible en el host del worker.") from error
        except Exception as error:
            raise _QueryFailure("execution", "No se pudo completar la consulta de solo lectura en el host.") from error
        self._remaining(deadline)
        if result.returncode != 0:
            failure = self.provider_error(result.stderr)
            raise _QueryFailure(str(failure["error_code"]), str(failure["message"]))
        if result.stdout_truncated:
            raise _QueryFailure("output-limit", "AWS superó el límite de respuesta y la consulta quedó incompleta.")
        try:
            payload = json.loads(result.stdout)
        except (ValueError, TypeError, RecursionError) as error:
            raise _QueryFailure("invalid-output", "AWS devolvió una respuesta con formato inválido.") from error
        if not isinstance(payload, dict):
            raise _QueryFailure("invalid-output", "AWS devolvió una respuesta con formato inválido.")
        return payload

    def _dealer_zone(self, table: str, dealer_id: str, deadline: float) -> ZoneInfo:
        payload = self._query_page(table, (
            "--index-name", "dealer_id-minimal-index", "--limit", "2",
            "--key-condition-expression", "#dealer = :dealer",
            "--projection-expression", "#dealer,#zone",
            "--expression-attribute-names", _encoded({"#dealer": "dealer_id", "#zone": "time_zone"}),
            "--expression-attribute-values", _encoded({":dealer": {"S": dealer_id}}),
            "--query", "{Items:Items,LastEvaluatedKey:LastEvaluatedKey}",
        ), deadline)
        records = payload.get("Items")
        if not isinstance(records, list):
            raise _QueryFailure("invalid-output", "AWS devolvió una configuración de dealer inválida.")
        if _cursor(payload.get("LastEvaluatedKey")) is not None or len(records) > 1:
            raise _QueryFailure("dealer-config-ambiguous", "No se pudo confirmar una única configuración de zona horaria para el dealer.")
        if not records:
            raise _QueryFailure("dealer-config-missing", "No se encontró la configuración del dealer en el entorno configurado.")
        record = records[0]
        if not isinstance(record, dict) or record.get("dealer_id") != {"S": dealer_id}:
            raise _QueryFailure("invalid-output", "La configuración devuelta no coincide con el dealer solicitado.")
        zone = record.get("time_zone")
        if not isinstance(zone, dict) or set(zone) != {"S"} or not isinstance(zone["S"], str) or not 1 <= len(zone["S"]) <= 128 or not _ZONE_NAME.fullmatch(zone["S"]):
            raise _QueryFailure("dealer-time-zone", "El dealer no tiene una zona horaria válida; no se asumió UTC.")
        try:
            return ZoneInfo(zone["S"])
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise _QueryFailure("dealer-time-zone", "La zona horaria del dealer no está disponible; no se asumió UTC.") from error

    def _task_count(self, table: str, dealer_id: str, lower: str, upper: str, deadline: float) -> tuple[int, int]:
        fixed = (
            "--index-name", "dealer_id-planned_start_at-index",
            "--select", "COUNT",
            "--key-condition-expression", "#dealer = :dealer AND #planned BETWEEN :lower AND :upper",
            "--expression-attribute-names", _encoded({"#dealer": "dealer_id", "#planned": "planned_start_at"}),
            "--expression-attribute-values", _encoded({":dealer": {"S": dealer_id}, ":lower": {"S": lower}, ":upper": {"S": upper}}),
            "--query", "{Count:Count,ScannedCount:ScannedCount,LastEvaluatedKey:LastEvaluatedKey}",
        )
        total = evaluated = 0
        cursor: dict[str, object] | None = None
        seen: set[str] = set()
        for page in range(1, MAX_PAGES + 1):
            limit = min(PAGE_LIMIT, MAX_EVALUATED - evaluated)
            if limit <= 0:
                break
            arguments = (*fixed, "--limit", str(limit))
            if cursor is not None:
                arguments += ("--exclusive-start-key=" + _encoded(cursor),)
            payload = self._query_page(table, arguments, deadline)
            count = _count(payload.get("Count"), limit)
            scanned = _count(payload.get("ScannedCount"), limit)
            if count != scanned:
                raise _QueryFailure("invalid-output", "AWS devolvió conteos incompatibles con una consulta sin filtros.")
            total += count
            evaluated += scanned
            cursor = _cursor(payload.get("LastEvaluatedKey"))
            if cursor is None:
                return total, page
            fingerprint = _encoded(cursor)
            if fingerprint in seen:
                raise _QueryFailure("incomplete-pagination", "AWS repitió una página; se detuvo la consulta sin confirmar el total.")
            seen.add(fingerprint)
        raise _QueryFailure("incomplete-limit", "La consulta quedó incompleta al alcanzar el límite de páginas o registros evaluados.")
