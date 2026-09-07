"""Create bounded CSV attachments from structured, allowlisted AWS responses.

Nested DynamoDB numbers are serialized as JSON strings so Decimal precision is
preserved. Scalar numbers retain their exact decimal representation in the CSV.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation


MAX_CSV_BYTES = 128 * 1024
_MISSING = object()
_LIMITS = {"list-dynamodb": 25, "describe-dynamodb": 1, "scan-dynamodb": 10, "list-log-groups": 25, "read-logs": 20, "count-planned-tasks": 1}
_FILENAMES = {
    "list-dynamodb": "dynamodb-tablas.csv", "describe-dynamodb": "dynamodb-detalle.csv",
    "scan-dynamodb": "dynamodb-registros.csv", "list-log-groups": "cloudwatch-grupos.csv",
    "read-logs": "cloudwatch-logs.csv",
    "count-planned-tasks": "tareas-planeadas.csv",
}


class CsvExportError(ValueError):
    """The structured response cannot be represented safely as CSV."""


class CsvTooLargeError(CsvExportError):
    """The complete CSV exceeds the attachment budget; no cells are truncated."""


@dataclass(frozen=True, slots=True)
class CsvExport:
    attachment: dict[str, str]
    row_count: int
    partial: bool


def build_csv_attachment(
    action: str,
    payload: dict[str, object],
    *,
    row_limit: int,
    redact: Callable[[object], object],
) -> CsvExport:
    if action not in _LIMITS or not isinstance(row_limit, int) or isinstance(row_limit, bool) or row_limit < 1:
        raise CsvExportError("La operación no permite exportar CSV.")
    limit = min(row_limit, _LIMITS[action])
    headers, rows, partial = _rows(action, payload, limit)
    safe_headers = [_cell(redact(header)) for header in headers]
    if len(safe_headers) != len(set(safe_headers)):
        raise CsvExportError("Dos columnas coinciden después de proteger sus encabezados; no se generó el CSV.")
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\r\n")
    writer.writerow(safe_headers)
    _check_size(stream)
    for row in rows:
        safe_row = redact(row)
        if not isinstance(safe_row, dict):
            raise CsvExportError("El registro CSV tiene un formato inválido.")
        writer.writerow([_cell(safe_row.get(header, _MISSING)) for header in headers])
        _check_size(stream)
    content = stream.getvalue().encode("utf-8-sig")
    return CsvExport(
        {
            "filename": _FILENAMES[action], "content_type": "text/csv",
            "content_base64": base64.b64encode(content).decode("ascii"),
            "sha256": hashlib.sha256(content).hexdigest(),
        },
        len(rows), partial,
    )


def _check_size(stream: io.StringIO) -> None:
    if len(stream.getvalue().encode("utf-8-sig")) > MAX_CSV_BYTES:
        raise CsvTooLargeError("El CSV supera 128 KiB; no se adjuntó un archivo recortado.")


def _rows(action: str, payload: dict[str, object], limit: int) -> tuple[list[str], list[dict[str, object]], bool]:
    if action == "count-planned-tasks":
        record = payload["Summary"]
        headers = ["dealer_id", "date", "time_zone", "count", "source"]
        if not isinstance(record, dict) or set(record) != set(headers) or record["count"] is None:
            raise CsvExportError("El resumen de tareas no tiene un formato válido.")
        row = {key: _number(record[key]) if key == "count" else _string(record[key]) for key in headers}
        return headers, [row], False
    if action == "describe-dynamodb":
        record = payload["Table"]
        if not isinstance(record, dict):
            raise CsvExportError("La tabla no tiene un formato válido.")
        return ["table_name", "status", "item_count_approximate", "size_bytes_approximate", "billing_mode"], [{
            "table_name": _string(record["TableName"]), "status": _string(record["TableStatus"]),
            "item_count_approximate": _number(record.get("ItemCount")),
            "size_bytes_approximate": _number(record.get("TableSizeBytes")),
            "billing_mode": _string(record.get("BillingMode") or "PROVISIONED"),
        }], False
    key = {"scan-dynamodb": "Items", "read-logs": "Events", "list-dynamodb": "TableNames", "list-log-groups": "Groups"}[action]
    records = payload[key]
    if not isinstance(records, list):
        raise CsvExportError("AWS no devolvió una lista válida para el CSV.")
    partial = len(records) > limit or bool(payload.get("LastEvaluatedKey" if action == "scan-dynamodb" else "NextToken"))
    records = records[:limit]
    if action == "list-dynamodb":
        return ["table_name"], [{"table_name": _string(name)} for name in records], partial
    if not all(isinstance(record, dict) for record in records):
        raise CsvExportError("AWS devolvió un registro inválido para el CSV.")
    if action == "list-log-groups":
        return ["log_group", "retention_days"], [{
            "log_group": _string(record["Name"]), "retention_days": _number(record.get("RetentionDays")),
        } for record in records], partial
    if action == "read-logs":
        rows = []
        for record in records:
            timestamp = record["Timestamp"]
            if not isinstance(timestamp, int) or isinstance(timestamp, bool) or not 0 <= timestamp <= 253_402_300_799_000:
                raise CsvExportError("El evento tiene una fecha inválida.")
            rows.append({
                "timestamp_utc": datetime.fromtimestamp(timestamp / 1000, UTC).isoformat(timespec="milliseconds"),
                "message": _string(record["Message"]),
            })
        return ["timestamp_utc", "message"], rows, partial
    rows = [{name: _attribute(value) for name, value in record.items()} for record in records]
    headers = sorted({name for row in rows for name in row})
    if not headers:
        return ["record"], [{"record": {}} for _ in rows], partial
    return headers, rows, partial


def _attribute(value: object) -> object:
    if not isinstance(value, dict) or len(value) != 1:
        raise CsvExportError("El atributo DynamoDB no tiene un tipo válido.")
    kind, data = next(iter(value.items()))
    if kind in {"S", "B"}:
        return _string(data)
    if kind == "N":
        try:
            number = Decimal(_string(data))
        except InvalidOperation as error:
            raise CsvExportError("DynamoDB devolvió un número inválido.") from error
        if not number.is_finite():
            raise CsvExportError("DynamoDB devolvió un número no finito.")
        return number
    if kind == "BOOL" and isinstance(data, bool):
        return data
    if kind == "NULL" and data is True:
        return None
    if kind == "M" and isinstance(data, dict):
        return {name: _attribute(item) for name, item in data.items()}
    if kind in {"L", "SS", "NS", "BS"} and isinstance(data, list):
        return [_attribute(item if kind == "L" else {kind[0]: item}) for item in data]
    raise CsvExportError("DynamoDB devolvió un tipo de atributo inválido.")


def _cell(value: object) -> str:
    if value is _MISSING:
        return ""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, Decimal)):
        return str(value)
    if isinstance(value, (list, dict)):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    if not isinstance(value, str):
        raise CsvExportError("El valor CSV no tiene un tipo válido.")
    # CSV quoting does not prevent spreadsheet formula execution. Prefix both
    # headers and strings; genuine numeric values took the typed branch above.
    if value.lstrip().startswith(("=", "+", "-", "@", "＝", "＋", "－", "＠")) or value.lstrip(" ").startswith(("\t", "\r", "\n")):
        return "'" + value
    return value


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise CsvExportError("AWS devolvió un texto inválido.")
    return value


def _number(value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise CsvExportError("AWS devolvió una cantidad inválida.")
    return value
