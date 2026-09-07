from __future__ import annotations

import json
import unittest
import os
from dataclasses import replace
from unittest.mock import patch

from worker.aws_queries import AwsQueries, _excerpt
from worker.config import WorkerSettings
from worker.processes import CommandResult, CommandTimedOut


class FakeRunner:
    def __init__(self, result: CommandResult | Exception) -> None:
        self.result = result
        self.calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def run(self, argv, **kwargs):
        self.calls.append((tuple(argv), kwargs))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class AwsQueriesTests(unittest.TestCase):
    def test_csv_attachment_uses_structured_full_values_and_existing_page_limits(self) -> None:
        import base64
        import csv
        import io
        content = "x" * 1800
        runner = FakeRunner(CommandResult(0, json.dumps({"Items": [{"text": {"S": content}}]})))
        report = AwsQueries(self.settings(), runner=runner).query("scan-dynamodb", table="Tasks", output_format="csv")
        rows = list(csv.reader(io.StringIO(base64.b64decode(report["attachment"]["content_base64"]).decode("utf-8-sig"))))
        self.assertEqual(rows[1][0], content)
        self.assertIn("no es una exportación de toda la tabla", report["message"])
        self.assertNotIn("1000", report["message"])
        self.assertEqual(runner.calls[0][0][runner.calls[0][0].index("--limit") + 1], "10")

    def test_invalid_csv_format_never_executes_and_failure_never_has_attachment(self) -> None:
        runner = FakeRunner(CommandResult(1, "", "AccessDenied"))
        service = AwsQueries(self.settings(), runner=runner)
        for invalid in ("xlsx", "CSV", None, [], 1):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                service.query("list-dynamodb", output_format=invalid)
        self.assertEqual(runner.calls, [])
        self.assertNotIn("attachment", service.query("list-dynamodb", output_format="csv"))
        runner.result = CommandResult(0, json.dumps({"Events": [{"Timestamp": 1_800_000_000_000, "Message": "é" * 70_000}]}))
        report = service.query("read-logs", log_group="/aws/tasks", output_format="csv")
        self.assertEqual(report["error_code"], "csv-too-large")
        self.assertNotIn("attachment", report)
    def test_large_plain_log_excerpt_is_bounded_without_suffix_rescanning(self) -> None:
        text, truncated = _excerpt("a" * 65_536, 300)
        self.assertEqual(text, "a" * 299 + "…")
        self.assertTrue(truncated)

    def test_authorization_credentials_are_redacted_before_generic_assignment(self) -> None:
        for header in ("Authorization: Bearer opaque-canary-123", "Authorization: Basic dXNlcjpwYXNzd29yZA=="):
            with self.subTest(header=header):
                text, _ = _excerpt(header, 300)
                self.assertNotIn("opaque-canary-123", text)
                self.assertNotIn("dXNlcjpwYXNzd29yZA==", text)
                self.assertIn("REDACTADO", text)

    def test_default_runner_uses_profile_home_without_inherited_aws_keys_or_endpoints(self) -> None:
        with patch.dict(os.environ, {
            "AWS_ACCESS_KEY_ID": "synthetic-id", "AWS_SECRET_ACCESS_KEY": "synthetic-key",
            "AWS_SESSION_TOKEN": "synthetic-token", "AWS_ENDPOINT_URL": "https://invalid.example",
            "AWS_ENDPOINT_URL_DYNAMODB": "https://invalid.example", "WORKER_PASSWORD": "internal-password",
        }):
            environment = AwsQueries(self.settings()).runner.environment
        self.assertIsInstance(environment, dict)
        for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_ENDPOINT_URL", "AWS_ENDPOINT_URL_DYNAMODB", "WORKER_PASSWORD"):
            self.assertNotIn(key, environment)
        self.assertIn("HOME", environment)
        self.assertEqual(environment["AWS_IGNORE_CONFIGURED_ENDPOINT_URLS"], "true")

    def settings(self, **kwargs) -> WorkerSettings:
        return WorkerSettings(
            host="127.0.0.1", port=4097, username="worker", password="private-password",
            aws_enabled=True, **kwargs,
        )

    def test_default_gate_never_invokes_cli(self) -> None:
        runner = FakeRunner(CommandResult(0, "{}"))
        service = AwsQueries(replace(self.settings(), aws_enabled=False), runner=runner)
        self.assertEqual(service.query("list-dynamodb")["state"], "disabled")
        self.assertEqual(runner.calls, [])

    def test_metadata_is_projected_and_never_exposes_extra_fields(self) -> None:
        runner = FakeRunner(CommandResult(0, json.dumps({
            "TableNames": ["Tasks"],
            "SecretAccessKey": "must-not-escape", "UserId": "private-user-id",
        })))
        report = AwsQueries(self.settings(), runner=runner).query("list-dynamodb")
        self.assertIn("Tasks", report["message"])
        self.assertNotIn("must-not-escape", json.dumps(report))
        self.assertNotIn("private-user-id", json.dumps(report))
        argv, kwargs = runner.calls[0]
        self.assertIn("list-tables", argv)
        self.assertIn("--query", argv)
        self.assertIn("--no-cli-pager", argv)
        self.assertEqual(kwargs["timeout"], 20.0)
        self.assertNotIn("stdout_path", kwargs)
        self.assertNotIn("stderr_path", kwargs)

    def test_lists_log_groups_with_explicit_limit_and_more_results(self) -> None:
        runner = FakeRunner(CommandResult(0, json.dumps({
            "Groups": [{"Name": "/aws/lambda/tasks", "RetentionDays": 7,
                           "Environment": {"Variables": {"PASSWORD": "must-not-escape"}}}],
            "NextToken": "private-next-token",
        })))
        report = AwsQueries(self.settings(), runner=runner).query("list-log-groups")
        self.assertTrue(report["truncated"])
        self.assertIn("parcial", report["message"])
        self.assertIn("25", report["message"])
        self.assertNotIn("must-not-escape", json.dumps(report))
        self.assertNotIn("private-next-token", json.dumps(report))
        argv = runner.calls[0][0]
        self.assertEqual(argv[argv.index("--limit") + 1], "25")
        self.assertIn("--no-paginate", argv)
        self.assertNotIn("Environment", argv[argv.index("--query") + 1])

    def test_dynamodb_describe_accepts_only_a_table_name_as_one_argument(self) -> None:
        runner = FakeRunner(CommandResult(0, json.dumps({"Table": {
            "TableName": "Tasks", "TableStatus": "ACTIVE", "ItemCount": 4,
            "TableSizeBytes": 128, "BillingMode": "PAY_PER_REQUEST",
        }})))
        report = AwsQueries(self.settings(), runner=runner).query("describe-dynamodb", table="Tasks")
        self.assertIn("aproximado", report["message"])
        self.assertIn("--table-name=Tasks", runner.calls[0][0])
        for action, table in (("delete-table", "Tasks"), ("describe-dynamodb", "Tasks; whoami"),
                              ("describe-dynamodb", "file:///secret"), ("identity", "Tasks")):
            with self.subTest(action=action, table=table):
                with self.assertRaises(ValueError):
                    AwsQueries(self.settings(), runner=runner).query(action, table=table)
        self.assertEqual(len(runner.calls), 1)

    def test_table_list_and_empty_results_are_bounded(self) -> None:
        runner = FakeRunner(CommandResult(0, json.dumps({"TableNames": [], "NextToken": None})))
        report = AwsQueries(self.settings(), runner=runner).query("list-dynamodb")
        self.assertFalse(report["truncated"])
        self.assertIn("0", report["message"])

    def test_errors_are_sanitized_and_do_not_forward_cli_output(self) -> None:
        for result, expected in (
            (CommandResult(1, "secret-stdout", "AccessDenied secret-stderr"), "access-denied"),
            (CommandResult(1, "secret-stdout", "ExpiredToken secret-stderr"), "authentication"),
            (CommandResult(1, "secret-stdout", "ResourceNotFoundException secret-stderr"), "not-found"),
            (CommandResult(0, "secret-invalid-json"), "invalid-output"),
            (CommandResult(0, "{}", stdout_truncated=True), "output-limit"),
            (CommandTimedOut("secret-timeout"), "timeout"),
            (FileNotFoundError("secret-path"), "unavailable"),
            (RuntimeError("secret-provider-error"), "execution"),
        ):
            with self.subTest(expected=expected):
                report = AwsQueries(self.settings(), runner=FakeRunner(result)).query("list-dynamodb")
                self.assertEqual(report["error_code"], expected)
                self.assertNotIn("secret", json.dumps(report))

    def test_sts_lambda_and_mutations_are_not_public_actions(self) -> None:
        runner = FakeRunner(CommandResult(0, "{}"))
        service = AwsQueries(self.settings(), runner=runner)
        for action in ("identity", "list-lambdas", "delete-table", "put-item", "update-item", "delete-log-group", "start-query"):
            with self.subTest(action=action), self.assertRaises(ValueError):
                service.query(action)
        self.assertEqual(runner.calls, [])

    def test_scan_has_one_bounded_page_and_redacts_common_credentials(self) -> None:
        runner = FakeRunner(CommandResult(0, json.dumps({
            "Items": [{"id": {"S": "task-1"}, "password": {"S": "secret-value"}, "details": {"S": "@everyone " + "x" * 1400}}] * 12,
            "ScannedCount": 10, "LastEvaluatedKey": {"id": "private-token"},
        })))
        report = AwsQueries(self.settings(), runner=runner).query("scan-dynamodb", table="Tasks")
        argv = runner.calls[0][0]
        self.assertIn("scan", argv)
        self.assertIn("--no-paginate", argv)
        self.assertEqual(argv[argv.index("--limit") + 1], "10")
        self.assertEqual(report["limit"], 10)
        self.assertTrue(report["truncated"])
        self.assertIn("no es un listado completo", report["message"])
        self.assertNotIn("secret-value", report["message"])
        self.assertNotIn("private-token", report["message"])
        self.assertNotIn("@everyone", report["message"])
        self.assertLess(len(report["message"]), 11_000)

    def test_logs_use_last_hour_no_unmask_and_bounded_sanitized_excerpts(self) -> None:
        runner = FakeRunner(CommandResult(0, json.dumps({
            "Events": [{"Timestamp": 1_800_000_000_000, "Message": "password=secret-value @everyone " + "x" * 600}],
            "NextToken": "private-token",
        })))
        report = AwsQueries(self.settings(), runner=runner, clock=lambda: 1_800_000_000).query("read-logs", log_group="/aws/lambda/tasks")
        argv = runner.calls[0][0]
        for flag in ("--no-paginate", "--no-unmask", "--no-start-from-head", "--log-group-name=/aws/lambda/tasks"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--start-time") + 1], "1799996400000")
        self.assertEqual(argv[argv.index("--end-time") + 1], "1800000000000")
        self.assertEqual(argv[argv.index("--limit") + 1], "20")
        self.assertTrue(report["truncated"])
        self.assertNotIn("secret-value", report["message"])
        self.assertNotIn("@everyone", report["message"])
        self.assertNotIn("private-token", report["message"])
        self.assertLess(len(next(line for line in report["message"].splitlines() if line.startswith("- "))), 340)
        for kwargs in ({"log_group": "x; whoami"}, {"table": "Tasks"}, {"log_group": "/group", "table": "Tasks"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                AwsQueries(self.settings(), runner=runner).query("read-logs", **kwargs)
        self.assertEqual(len(runner.calls), 1)
