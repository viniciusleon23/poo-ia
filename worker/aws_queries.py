"""Bounded DynamoDB and CloudWatch reads in the authenticated host worker.

Only named resources enter fixed CLI operations. Records and log excerpts are
bounded, with common credential patterns redacted; this is not a guarantee that
every sensitive value can be recognized. Credentials stay in the host profile.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from .config import WorkerSettings
from .aws_csv import CsvExportError, CsvTooLargeError, build_csv_attachment
from .processes import CommandRunner, CommandTimedOut, SubprocessCommandRunner


MAX_ITEMS = 25
MAX_RECORDS = 10
MAX_EVENTS = 20
MAX_CAPTURE_BYTES = 65_536
MAX_CSV_CAPTURE_BYTES = 1_048_576
_TABLE_NAME = re.compile(r"[A-Za-z0-9_.-]{3,255}\Z")
_LOG_GROUP = re.compile(r"[A-Za-z0-9._/#-]{1,512}\Z")
_OPERATIONS = {
    "list-dynamodb": (
        "dynamodb", "list-tables", "{TableNames:TableNames,NextToken:NextToken}",
    ),
    "describe-dynamodb": (
        "dynamodb", "describe-table",
        "{Table:{TableName:Table.TableName,TableStatus:Table.TableStatus,ItemCount:Table.ItemCount,TableSizeBytes:Table.TableSizeBytes,BillingMode:Table.BillingModeSummary.BillingMode}}",
    ),
    "scan-dynamodb": (
        "dynamodb", "scan", "{Items:Items,ScannedCount:ScannedCount,LastEvaluatedKey:LastEvaluatedKey}",
    ),
    "list-log-groups": (
        "logs", "describe-log-groups",
        "{Groups:logGroups[].{Name:logGroupName,RetentionDays:retentionInDays},NextToken:nextToken}",
    ),
    "read-logs": (
        "logs", "filter-log-events", "{Events:events[].{Timestamp:timestamp,Message:message},NextToken:nextToken}",
    ),
}


def _failure(code: str, message: str, *, state: str = "failed") -> dict[str, object]:
    return {"state": state, "error_code": code, "message": message}


class AwsQueries:
    def __init__(self, settings: WorkerSettings, *, runner: CommandRunner | None = None, clock: Callable[[], float] = time.time) -> None:
        self.settings = settings
        self.runner = runner or SubprocessCommandRunner(environment=_aws_environment())
        self.clock = clock

    def query(self, action: str, *, table: str | None = None, log_group: str | None = None, output_format: str = "text") -> dict[str, object]:
        if not isinstance(output_format, str) or output_format not in {"text", "csv"}:
            raise ValueError("El formato AWS debe ser text o csv.")
        if not self.settings.aws_enabled:
            return _failure("disabled", "Las consultas AWS están desactivadas en el worker.", state="disabled")
        if not isinstance(action, str) or action not in _OPERATIONS:
            raise ValueError("La operación AWS no está permitida.")
        if action in {"describe-dynamodb", "scan-dynamodb"}:
            if not isinstance(table, str) or not _TABLE_NAME.fullmatch(table):
                raise ValueError("Indica un nombre válido de tabla DynamoDB.")
        elif table is not None:
            raise ValueError("Esta operación AWS no acepta una tabla.")
        if action == "read-logs":
            if not isinstance(log_group, str) or not _LOG_GROUP.fullmatch(log_group):
                raise ValueError("Indica un nombre válido de grupo CloudWatch.")
        elif log_group is not None:
            raise ValueError("Esta operación AWS no acepta un grupo de logs.")

        service, operation, projection = _OPERATIONS[action]
        argv = [
            self.settings.aws_executable,
            "--profile", self.settings.aws_profile,
            "--region", self.settings.aws_region,
            "--output", "json", "--no-cli-pager", "--no-cli-auto-prompt",
            "--cli-connect-timeout", "5", "--cli-read-timeout", "15",
            service, operation,
        ]
        if action == "list-dynamodb":
            argv.extend(("--max-items", str(MAX_ITEMS), "--page-size", str(MAX_ITEMS)))
        elif action == "list-log-groups":
            argv.extend(("--no-paginate", "--limit", str(MAX_ITEMS)))
        elif action == "scan-dynamodb":
            argv.extend(("--no-paginate", "--limit", str(MAX_RECORDS), "--no-consistent-read"))
        elif action == "read-logs":
            end = int(self.clock() * 1000)
            argv.extend((
                "--no-paginate", "--limit", str(MAX_EVENTS),
                "--start-time", str(max(0, end - 3_600_000)), "--end-time", str(end),
                "--no-start-from-head", "--no-unmask",
            ))
        if table is not None:
            # A single option=value argument cannot turn a leading '-' in a
            # legitimate table name into an additional AWS CLI flag.
            argv.append(f"--table-name={table}")
        if log_group is not None:
            argv.append(f"--log-group-name={log_group}")
        argv.extend(("--query", projection))
        try:
            result = self.runner.run(
                tuple(argv), timeout=self.settings.aws_query_timeout_seconds,
                capture_limit_bytes=MAX_CSV_CAPTURE_BYTES if output_format == "csv" else MAX_CAPTURE_BYTES,
            )
        except CommandTimedOut:
            return _failure("timeout", "AWS no respondió dentro del límite de tiempo de la consulta.")
        except OSError:
            return _failure("unavailable", "AWS CLI no está disponible en el host del worker.")
        except Exception:
            return _failure("execution", "No se pudo ejecutar la consulta AWS en el host del worker.")
        if result.returncode != 0:
            return self._provider_error(result.stderr)
        if result.stdout_truncated:
            return _failure("output-limit", "La respuesta AWS superó el límite de salida; no se muestra un resultado incompleto.")
        try:
            payload = json.loads(result.stdout)
            if not isinstance(payload, dict):
                raise ValueError
            if output_format == "csv":
                return self._csv_report(action, payload, table=table, log_group=log_group)
            message, truncated = self._render(action, payload, table=table, log_group=log_group)
        except CsvTooLargeError as error:
            return _failure("csv-too-large", str(error))
        except CsvExportError as error:
            return _failure("csv-invalid", str(error))
        except (ValueError, TypeError, KeyError, RecursionError):
            return _failure("invalid-output", "AWS devolvió una respuesta que no cumple el formato esperado.")
        return {
            "state": "succeeded", "action": action, "message": message,
            "truncated": truncated, "limit": (
                MAX_ITEMS if action.startswith("list-") else MAX_RECORDS if action == "scan-dynamodb"
                else MAX_EVENTS if action == "read-logs" else 1
            ),
        }

    def _csv_report(self, action: str, payload: dict[str, object], *, table: str | None, log_group: str | None) -> dict[str, object]:
        limit = MAX_ITEMS if action.startswith("list-") else MAX_RECORDS if action == "scan-dynamodb" else MAX_EVENTS if action == "read-logs" else 1
        export = build_csv_attachment(action, payload, row_limit=limit, redact=_redact_record)
        region = _identifier(self.settings.aws_region)
        subject = {
            "list-dynamodb": "Tablas DynamoDB", "describe-dynamodb": "Detalle DynamoDB",
            "scan-dynamodb": "Registros DynamoDB", "list-log-groups": "Grupos CloudWatch",
            "read-logs": "Logs CloudWatch",
        }[action]
        resource = f" de {_identifier(table or log_group)}" if table or log_group else ""
        lines = [f"CSV de {subject}{resource} en {region}: {export.row_count} filas (límite: {limit})."]
        if action == "scan-dynamodb":
            lines.append("Una página de hasta 10 registros evaluados, con consistencia eventual; no es una exportación de toda la tabla.")
        elif action == "read-logs":
            lines.append("Una página de hasta 20 eventos de la última hora, más recientes primero; no es el historial completo.")
        if export.partial:
            lines.append("Resultado parcial: AWS indica más páginas o se alcanzó el límite de filas.")
        elif action.startswith("list-"):
            lines.append("AWS no indicó más páginas en esta respuesta.")
        lines.append("CSV UTF-8 para Excel, sin recorte de celdas y con límite total de 128 KiB. Se protegen fórmulas y patrones comunes de credenciales; la redacción puede no reconocer todos los datos sensibles.")
        return {
            "state": "succeeded", "action": action, "message": "\n".join(lines),
            "truncated": export.partial, "limit": limit, "attachment": export.attachment,
        }

    @staticmethod
    def _provider_error(stderr: str) -> dict[str, object]:
        lowered = stderr.casefold()
        if any(code in lowered for code in ("accessdenied", "unauthorizedoperation", "not authorized")):
            return _failure("access-denied", "AWS denegó permiso para esta consulta de solo lectura.")
        if any(code in lowered for code in (
            "expiredtoken", "invalidclienttokenid", "unrecognizedclient", "unable to locate credentials",
            "invalidaccesskeyid", "signaturedoesnotmatch", "config profile",
        )):
            return _failure("authentication", "La sesión o el perfil AWS del host requiere revisión o renovación.")
        if "resourcenotfound" in lowered:
            return _failure("not-found", "AWS no encontró el recurso en la región configurada.")
        if any(code in lowered for code in ("throttl", "toomanyrequests", "requestlimitexceeded")):
            return _failure("rate-limit", "AWS limitó temporalmente las consultas; vuelve a intentarlo después.")
        return _failure("provider-error", "No se pudo completar la consulta AWS. Revisa el acceso y la conectividad del host.")

    def _render(self, action: str, payload: dict[str, object], *, table: str | None, log_group: str | None) -> tuple[str, bool]:
        region = _identifier(self.settings.aws_region)
        if action in {"scan-dynamodb", "read-logs"}:
            return self._render_data(action, payload, table=table, log_group=log_group, region=region)
        if action == "describe-dynamodb":
            record = payload["Table"]
            if not isinstance(record, dict):
                raise ValueError
            return (
                f"Tabla DynamoDB {_identifier(record['TableName'])} en {region}:\n"
                f"Estado: {_identifier(record['TableStatus'])}\n"
                f"Elementos (conteo aproximado de AWS): {_number(record.get('ItemCount'))}\n"
                f"Tamaño aproximado: {_number(record.get('TableSizeBytes'))} bytes\n"
                f"Facturación: {_identifier(record.get('BillingMode') or 'PROVISIONED')}\n"
                "Consulta de metadatos; no se leyeron los registros de la tabla."
            ), False
        key = "Groups" if action == "list-log-groups" else "TableNames"
        records = payload[key]
        if not isinstance(records, list):
            raise ValueError
        truncated = bool(payload.get("NextToken")) or len(records) > MAX_ITEMS
        visible = records[:MAX_ITEMS]
        label = "Grupos CloudWatch" if action == "list-log-groups" else "Tablas DynamoDB"
        lines = [f"{label} en {region}: {len(visible)} mostradas (límite: {MAX_ITEMS})."]
        for record in visible:
            if action == "list-log-groups":
                if not isinstance(record, dict):
                    raise ValueError
                lines.append(
                    f"- {_identifier(record['Name'])}: retención "
                    f"{_number(record.get('RetentionDays'))} días"
                )
            else:
                lines.append(f"- {_identifier(record)}")
        lines.append(
            f"Resultado parcial: AWS indica más elementos; esta consulta se limita a {MAX_ITEMS}."
            if truncated else f"Paginación limitada a {MAX_ITEMS}; AWS no indicó más elementos."
        )
        return "\n".join(lines), truncated

    @staticmethod
    def _render_data(action: str, payload: dict[str, object], *, table: str | None, log_group: str | None, region: str) -> tuple[str, bool]:
        scanning = action == "scan-dynamodb"
        records = payload["Items" if scanning else "Events"]
        if not isinstance(records, list):
            raise ValueError
        limit = MAX_RECORDS if scanning else MAX_EVENTS
        truncated = bool(payload.get("LastEvaluatedKey" if scanning else "NextToken")) or len(records) > limit
        lines = [
            f"Registros DynamoDB de {_identifier(table)} en {region}: {min(len(records), limit)} mostrados."
            if scanning else f"Logs CloudWatch de {_identifier(log_group)} en {region}: {min(len(records), limit)} eventos de la última hora, más recientes primero."
        ]
        for index, record in enumerate(records[:limit], 1):
            if not isinstance(record, dict):
                raise ValueError
            if scanning:
                content = json.dumps(_redact_record(record), ensure_ascii=False, separators=(",", ":"))
                content, clipped = _excerpt(content, 1000, redact=False)
                lines.append(f"{index}. {content}")
            else:
                timestamp = record["Timestamp"]
                if not isinstance(timestamp, int) or isinstance(timestamp, bool) or not 0 <= timestamp <= 253_402_300_799_000:
                    raise ValueError
                moment = datetime.fromtimestamp(timestamp / 1000, UTC).isoformat(timespec="seconds")
                content, clipped = _excerpt(record["Message"], 300)
                lines.append(f"- {moment}: {content}")
            truncated = truncated or clipped
        lines.append(
            "Lectura limitada: hasta 10 registros evaluados en una sola página, con consistencia eventual; no es un listado completo. Máximo 1000 caracteres por registro."
            if scanning else "Lectura limitada: una página de hasta 20 eventos de la última hora; no es el historial completo. Máximo 300 caracteres por mensaje."
        )
        if truncated:
            lines.append("Resultado parcial: existen más páginas o se recortaron elementos/textos.")
        lines.append("Se ocultan patrones comunes de credenciales; esta redacción no detecta necesariamente todos los datos sensibles.")
        return "\n".join(lines), truncated


def _identifier(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._:/+=,@#-]{1,512}", value):
        raise ValueError
    return value.replace("@", "@\u200b")


def _number(value: object) -> str:
    if value is None:
        return "no disponible"
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError
    return str(value)


_SENSITIVE_KEY = re.compile(r"password|passwd|credential|secret|token|api[_-]?key|access[_-]?key|authorization|private[_-]?key", re.IGNORECASE)
_ASSIGNMENT = re.compile(
    r'''(?i)(?<![A-Za-z0-9_-])((?:["']?)(?:[a-z0-9_-]*(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|authorization)[a-z0-9_-]*)(?:["']?)\s*[:=]\s*)(?:"[^"\n]*"|'[^'\n]*'|[^\s,;]+)'''
)


def _redact_record(value: object) -> object:
    if isinstance(value, dict):
        return {key: "[REDACTADO]" if _SENSITIVE_KEY.search(key) else _redact_record(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_record(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _redact_text(value: str) -> str:
    # Mask the complete authentication value before generic key=value masking
    # can remove its scheme and leave an opaque credential behind.
    value = re.sub(r"(?i)\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/-]+=*", "[REDACTADO]", value)
    value = _ASSIGNMENT.sub(lambda match: match.group(1) + "[REDACTADO]", value)
    value = re.sub(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b", "[REDACTADO]", value)
    return re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", "[REDACTADO]", value)


def _excerpt(value: object, limit: int, *, redact: bool = True) -> tuple[str, bool]:
    if not isinstance(value, str):
        raise ValueError
    if redact:
        value = _redact_text(value)
    value = " ".join(value.split()).replace("@", "＠").replace("`", "ˋ")
    clipped = len(value) > limit
    return (value[:limit - 1] + "…" if clipped else value), clipped


def _aws_environment() -> dict[str, str]:
    """Use the host's default profile, never inherited keys or custom endpoints."""
    return {
        "HOME": str(Path.home()),
        "PATH": os.environ.get("PATH", os.defpath),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "AWS_PAGER": "",
        "AWS_CLI_AUTO_PROMPT": "off",
        "AWS_IGNORE_CONFIGURED_ENDPOINT_URLS": "true",
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_MAX_ATTEMPTS": "2",
    }
