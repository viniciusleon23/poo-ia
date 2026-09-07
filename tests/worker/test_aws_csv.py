from __future__ import annotations

import base64
import csv
import hashlib
import io
import unittest

from worker.aws_csv import CsvExportError, CsvTooLargeError, build_csv_attachment
from worker.aws_queries import _redact_record


def exported(action, payload, limit=10):
    result = build_csv_attachment(action, payload, row_limit=limit, redact=_redact_record)
    content = base64.b64decode(result.attachment["content_base64"], validate=True)
    rows = list(csv.reader(io.StringIO(content.decode("utf-8-sig"))))
    return result, content, rows


class AwsCsvTests(unittest.TestCase):
    def test_sanitized_header_collisions_are_rejected_and_unicode_formulas_protected(self) -> None:
        with self.assertRaises(CsvExportError):
            exported("scan-dynamodb", {"Items": [{"=x": {"S": "one"}, "'=x": {"S": "two"}}]})
        _, _, rows = exported("scan-dynamodb", {"Items": [{"name": {"S": "\t＠SUM(1)"}}]})
        self.assertTrue(rows[1][0].startswith("'"))
    def test_dynamodb_union_columns_typed_values_and_precise_numbers(self) -> None:
        result, content, rows = exported("scan-dynamodb", {"Items": [
            {"id": {"S": "one"}, "n": {"N": "-12345678901234567890.123456789"}, "active": {"BOOL": False}, "nil": {"NULL": True}, "nested": {"M": {"name": {"S": "José"}, "zero": {"N": "0"}}}},
            {"id": {"S": "two"}, "extra": {"L": [{"S": "a"}, {"N": "2"}]}},
        ], "LastEvaluatedKey": {"id": "private-token"}})
        self.assertTrue(content.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(result.attachment["sha256"], hashlib.sha256(content).hexdigest())
        self.assertEqual(result.attachment["content_type"], "text/csv")
        self.assertTrue(result.partial)
        self.assertEqual(result.row_count, 2)
        self.assertEqual(rows[0], ["active", "extra", "id", "n", "nested", "nil"])
        first = dict(zip(rows[0], rows[1]))
        self.assertEqual(first["n"], "-12345678901234567890.123456789")
        self.assertEqual(first["active"], "false")
        self.assertEqual(first["nil"], "null")
        self.assertIn("José", first["nested"])
        self.assertNotIn("private-token", content.decode("utf-8-sig"))

    def test_formula_injection_in_headers_and_strings_and_nested_redaction(self) -> None:
        _, content, rows = exported("scan-dynamodb", {"Items": [{
            "=HYPERLINK(1)": {"S": " \t=HYPERLINK(2)"},
            "minus_string": {"S": "-2"}, "minus_number": {"N": "-2"},
            "nested": {"M": {"token": {"S": "nested-canary"}}},
            "password": {"S": "password-canary"},
            "authorization": {"S": "Bearer opaque-canary"},
        }]})
        self.assertTrue(rows[0][0].startswith("'="))
        values = dict(zip(rows[0], rows[1]))
        self.assertTrue(values[rows[0][0]].startswith("'"))
        self.assertEqual(values["minus_string"], "'-2")
        self.assertEqual(values["minus_number"], "-2")
        for canary in ("nested-canary", "password-canary", "opaque-canary"):
            self.assertNotIn(canary, content.decode("utf-8-sig"))

    def test_logs_keep_full_multiline_text_beyond_chat_excerpt_limit(self) -> None:
        message = 'línea, "comillas"\n' + "x" * 1500
        result, _, rows = exported("read-logs", {"Events": [{"Timestamp": 1_800_000_000_123, "Message": message}]}, limit=20)
        self.assertEqual(rows[0], ["timestamp_utc", "message"])
        self.assertTrue(rows[1][0].endswith(".123+00:00"))
        self.assertEqual(rows[1][1], message)
        self.assertFalse(result.partial)

    def test_list_describe_and_empty_exports_have_stable_columns(self) -> None:
        for action, payload, headers in (
            ("list-dynamodb", {"TableNames": ["Tasks"]}, ["table_name"]),
            ("list-log-groups", {"Groups": [{"Name": "/aws/tasks", "RetentionDays": 7}]}, ["log_group", "retention_days"]),
            ("describe-dynamodb", {"Table": {"TableName": "Tasks", "TableStatus": "ACTIVE", "ItemCount": 0, "TableSizeBytes": 0}}, ["table_name", "status", "item_count_approximate", "size_bytes_approximate", "billing_mode"]),
            ("scan-dynamodb", {"Items": []}, ["record"]),
        ):
            with self.subTest(action=action):
                _, _, rows = exported(action, payload, limit=25)
                self.assertEqual(rows[0], headers)

    def test_row_limits_never_export_unrequested_records(self) -> None:
        result, _, rows = exported("scan-dynamodb", {"Items": [{"id": {"N": str(i)}} for i in range(12)]}, limit=10)
        self.assertEqual(len(rows), 11)
        self.assertTrue(result.partial)

    def test_oversized_csv_fails_instead_of_truncating_cells(self) -> None:
        with self.assertRaises(CsvTooLargeError):
            exported("read-logs", {"Events": [{"Timestamp": 1_800_000_000_000, "Message": "é" * 70_000}]}, limit=20)

    def test_remaining_dynamodb_types_and_invalid_numbers(self) -> None:
        _, _, rows = exported("scan-dynamodb", {"Items": [{
            "bin": {"B": "AQI="}, "bins": {"BS": ["AQI=", "AwQ="]},
            "strings": {"SS": ["a", "b"]}, "nums": {"NS": ["1.000", "2e5"]},
        }]})
        values = dict(zip(rows[0], rows[1]))
        self.assertEqual(values["bin"], "AQI=")
        self.assertEqual(values["nums"], '["1.000","2E+5"]')
        with self.assertRaises(ValueError):
            exported("scan-dynamodb", {"Items": [{"n": {"N": "NaN"}}]})
