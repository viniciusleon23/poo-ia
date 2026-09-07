from __future__ import annotations

import base64
import csv
import io
import json
import unittest
from datetime import UTC, datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from worker.aws_queries import AwsQueries
from worker.business_queries import planning_bounds
from worker.config import WorkerSettings
from worker.processes import CommandResult, CommandTimedOut


DEALER = "COMAZDCALC2"
REQUESTED_AT = datetime(2026, 9, 7, 3, 30, tzinfo=UTC).timestamp()
CURSOR = {"dealer_id": {"S": DEALER}, "task_id": {"S": "private-pagination-canary"}}


def dealer_config(zone="America/Mexico_City"):
    return {"Items": [{"dealer_id": {"S": DEALER}, "time_zone": {"S": zone},
                       "password": {"S": "private-config-canary"}}]}


def count_page(count=0, cursor=None):
    return {"Count": count, "ScannedCount": count, "LastEvaluatedKey": cursor}


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class ScriptedRunner:
    def __init__(self, responses, *, clock=None, delay=0):
        self.responses = list(responses)
        self.calls = []
        self.clock = clock
        self.delay = delay

    def run(self, argv, **kwargs):
        self.calls.append((tuple(argv), kwargs))
        if self.clock is not None:
            self.clock.now += self.delay
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, CommandResult):
            return response
        return CommandResult(0, json.dumps(response))


class BusinessQueriesTests(unittest.TestCase):
    def settings(self, **kwargs):
        values = {
            "host": "127.0.0.1", "port": 4097, "username": "worker", "password": "private-password",
            "aws_enabled": True, "aws_tasks_table": "Tasks-prod", "aws_dealer_config_table": "DealerConfig-prod",
            "aws_profile": "production", "aws_region": "us-east-1",
        }
        values.update(kwargs)
        return WorkerSettings(**values)

    def query(self, service, *, day="tomorrow", output_format="text", **kwargs):
        return service.query("count-planned-tasks", business_query={
            "dealer_id": DEALER, "day": day, "requested_at": REQUESTED_AT,
        }, output_format=output_format, **kwargs)

    def test_paginates_empty_page_and_counts_all_pages_without_returning_records(self):
        second_cursor = {"task_id": {"S": "private-second-cursor"}}
        runner = ScriptedRunner([
            dealer_config(), count_page(0, CURSOR), count_page(3, second_cursor),
            {**count_page(2), "Items": [{"secret": "private-item-canary"}]},
        ])
        report = self.query(AwsQueries(self.settings(), runner=runner))
        self.assertEqual(report["state"], "succeeded")
        self.assertTrue(report["complete"])
        self.assertEqual(report["count"], 5)
        self.assertEqual(report["pages"], 3)
        self.assertEqual(report["date"], "2026-09-07")
        self.assertEqual(report["time_zone"], "America/Mexico_City")
        self.assertIn("todos los tipos y estados", report["message"])
        self.assertIn("excluye tareas sin inicio planeado", report["message"])
        self.assertIn("consistencia eventual", report["message"])
        self.assertIn("Tasks-prod en us-east-1", report["message"])
        serialized = json.dumps(report)
        self.assertNotIn("private-", serialized)
        self.assertNotIn("Items", serialized)
        self.assertNotIn("LastEvaluatedKey", serialized)
        self.assertEqual(len(runner.calls), 4)
        config_argv = runner.calls[0][0]
        self.assertIn("--table-name=DealerConfig-prod", config_argv)
        self.assertEqual(config_argv[config_argv.index("--index-name") + 1], "dealer_id-minimal-index")
        self.assertEqual(config_argv[config_argv.index("--projection-expression") + 1], "#dealer,#zone")
        first_values = None
        for argv, kwargs in runner.calls[1:]:
            self.assertIn("--table-name=Tasks-prod", argv)
            self.assertIn("--no-paginate", argv)
            self.assertIn("--no-consistent-read", argv)
            self.assertEqual(argv[argv.index("--select") + 1], "COUNT")
            self.assertEqual(argv[argv.index("--limit") + 1], "500")
            self.assertEqual(argv[argv.index("--profile") + 1], "production")
            self.assertNotIn("--filter-expression", argv)
            self.assertNotIn("scan", argv)
            self.assertNotIn("stdout_path", kwargs)
            self.assertNotIn("stderr_path", kwargs)
            self.assertLessEqual(kwargs["capture_limit_bytes"], 65_536)
            values = json.loads(argv[argv.index("--expression-attribute-values") + 1])
            if first_values is None:
                first_values = values
            self.assertEqual(values, first_values)
        self.assertEqual(first_values, {
            ":dealer": {"S": DEALER}, ":lower": {"S": "2026-09-07T06:00:00"},
            ":upper": {"S": "2026-09-08T05:59:59Z"},
        })
        cursor_argument = next(arg for arg in runner.calls[2][0] if arg.startswith("--exclusive-start-key="))
        self.assertEqual(json.loads(cursor_argument.split("=", 1)[1]), CURSOR)

    def test_zero_is_success_only_after_last_page(self):
        runner = ScriptedRunner([dealer_config(), count_page(0, CURSOR), count_page(0)])
        report = self.query(AwsQueries(self.settings(), runner=runner))
        self.assertEqual(report["count"], 0)
        self.assertTrue(report["complete"])
        self.assertEqual(len(runner.calls), 3)

    def test_csv_is_one_summary_row_from_the_completed_count(self):
        runner = ScriptedRunner([dealer_config(), count_page(9)])
        report = self.query(AwsQueries(self.settings(), runner=runner), output_format="csv")
        content = base64.b64decode(report["attachment"]["content_base64"])
        rows = list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))
        self.assertEqual(rows, [{
            "dealer_id": DEALER, "date": "2026-09-07", "time_zone": "America/Mexico_City", "count": "9",
            "source": "DynamoDB:us-east-1:Tasks-prod/dealer_id-planned_start_at-index",
        }])
        self.assertTrue(content.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(report["attachment"]["filename"], "tareas-planeadas.csv")
        self.assertEqual(len(runner.calls), 2)
        self.assertNotIn(b"private-config-canary", content)

    def test_disabled_and_missing_environment_configuration_never_execute(self):
        for fields, state in (
            ({"aws_enabled": False}, "disabled"),
            ({"aws_tasks_table": None}, "failed"),
            ({"aws_dealer_config_table": None}, "failed"),
            ({"aws_tasks_table": "Tasks; whoami"}, "failed"),
        ):
            with self.subTest(fields=fields):
                runner = ScriptedRunner([])
                report = self.query(AwsQueries(self.settings(**fields), runner=runner))
                self.assertEqual(report["state"], state)
                self.assertNotIn("count", report)
                self.assertEqual(runner.calls, [])

    def test_invalid_requests_cannot_supply_commands_tables_or_expressions(self):
        valid = {"dealer_id": DEALER, "day": "tomorrow", "requested_at": REQUESTED_AT}
        cases = [None, [], {}, {**valid, "table": "Other"}, {**valid, "filter": "anything"}]
        cases.extend({**valid, "dealer_id": value} for value in ("", "x; whoami", "file:///secret", "@everyone", []))
        cases.extend({**valid, "day": value} for value in ("soon", "2026-02-30", "2026-9-7", "2026-09-07; x", None))
        cases.extend({**valid, "requested_at": value} for value in (True, "123", None, float("nan"), float("inf"), 10 ** 1000))
        runner = ScriptedRunner([])
        service = AwsQueries(self.settings(), runner=runner)
        for query in cases:
            with self.subTest(query=query), self.assertRaises(ValueError):
                service.query("count-planned-tasks", business_query=query)
        for kwargs in ({"table": "Tasks"}, {"log_group": "/aws/tasks"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.query(service, **kwargs)
        with self.assertRaises(ValueError):
            service.query("list-dynamodb", business_query=valid)
        with self.assertRaises(ValueError):
            self.query(service, output_format="xlsx")
        self.assertEqual(runner.calls, [])

    def test_dealer_must_have_one_matching_config_and_a_valid_time_zone(self):
        valid = dealer_config()["Items"][0]
        cases = (
            ({"Items": []}, "dealer-config-missing"),
            ({"Items": [valid, valid]}, "dealer-config-ambiguous"),
            ({"Items": [valid], "LastEvaluatedKey": CURSOR}, "dealer-config-ambiguous"),
            ({"Items": [{"dealer_id": {"S": "OTHER"}, "time_zone": {"S": "UTC"}}]}, "invalid-output"),
            ({"Items": [{"dealer_id": {"S": DEALER}}]}, "dealer-time-zone"),
            (dealer_config("Missing/CanaryZone"), "dealer-time-zone"),
            (dealer_config("../../private-canary"), "dealer-time-zone"),
            (dealer_config({"S": "UTC"}), "dealer-time-zone"),
            ({"Items": "private-canary"}, "invalid-output"),
        )
        for payload, error_code in cases:
            with self.subTest(error_code=error_code, payload=payload):
                runner = ScriptedRunner([payload])
                report = self.query(AwsQueries(self.settings(), runner=runner), output_format="csv")
                self.assertEqual(report["error_code"], error_code)
                self.assertFalse(report["complete"])
                self.assertNotIn("count", report)
                self.assertNotIn("attachment", report)
                self.assertNotIn("private-canary", json.dumps(report))
                self.assertEqual(len(runner.calls), 1)

    def test_total_deadline_includes_configuration_and_every_page(self):
        clock = FakeClock()
        runner = ScriptedRunner([dealer_config(), count_page(3, CURSOR), count_page(4)], clock=clock, delay=7)
        report = self.query(AwsQueries(self.settings(), runner=runner, monotonic=clock), output_format="csv")
        self.assertEqual(report["error_code"], "incomplete-timeout")
        self.assertFalse(report["complete"])
        self.assertNotIn("count", report)
        self.assertNotIn("attachment", report)
        self.assertEqual([kwargs["timeout"] for _, kwargs in runner.calls], [20, 13, 6])

    def test_configured_timeout_can_reduce_but_not_expand_twenty_second_budget(self):
        for configured, expected in ((3.0, 3.0), (60.0, 20.0)):
            clock = FakeClock()
            runner = ScriptedRunner([dealer_config(), count_page()], clock=clock)
            self.query(AwsQueries(self.settings(aws_query_timeout_seconds=configured), runner=runner, monotonic=clock))
            self.assertEqual(runner.calls[0][1]["timeout"], expected)

    def test_page_and_evaluated_limits_never_return_partial_totals(self):
        with patch("worker.business_queries.MAX_PAGES", 1):
            runner = ScriptedRunner([dealer_config(), count_page(4, CURSOR)])
            report = self.query(AwsQueries(self.settings(), runner=runner), output_format="csv")
        self.assertEqual(report["error_code"], "incomplete-limit")
        self.assertNotIn("count", report)
        self.assertNotIn("attachment", report)
        with patch("worker.business_queries.MAX_EVALUATED", 2):
            runner = ScriptedRunner([dealer_config(), count_page(2, CURSOR)])
            report = self.query(AwsQueries(self.settings(), runner=runner))
        self.assertEqual(report["error_code"], "incomplete-limit")
        argv = runner.calls[1][0]
        self.assertEqual(argv[argv.index("--limit") + 1], "2")
        with patch("worker.business_queries.MAX_EVALUATED", 2):
            runner = ScriptedRunner([dealer_config(), count_page(2)])
            report = self.query(AwsQueries(self.settings(), runner=runner))
        self.assertEqual(report["count"], 2)
        self.assertTrue(report["complete"])

    def test_malformed_counts_and_cursors_fail_without_exposing_provider_data(self):
        pages = [{"Count": value, "ScannedCount": 0} for value in (-1, True, "2", None, 501)]
        pages += [{"Count": 2, "ScannedCount": 1}, {"Count": 0}, count_page(0, "private-canary")]
        pages += [count_page(0, {"id": value}) for value in ({"N": "NaN"}, {"BOOL": True}, {"B": "?!bad"}, {"S": []}, {"S": ""})]
        for page in pages:
            with self.subTest(page=page):
                runner = ScriptedRunner([dealer_config(), page])
                report = self.query(AwsQueries(self.settings(), runner=runner), output_format="csv")
                self.assertEqual(report["error_code"], "invalid-output")
                self.assertNotIn("count", report)
                self.assertNotIn("attachment", report)
                self.assertNotIn("private-canary", json.dumps(report))

    def test_repeated_cursor_stops_instead_of_counting_forever(self):
        runner = ScriptedRunner([dealer_config(), count_page(2, CURSOR), count_page(2, CURSOR)])
        report = self.query(AwsQueries(self.settings(), runner=runner))
        self.assertEqual(report["error_code"], "incomplete-pagination")
        self.assertNotIn("count", report)
        self.assertEqual(len(runner.calls), 3)

    def test_pagination_preserves_typed_number_and_binary_keys_without_exposing_them(self):
        cursor = {
            "number_key": {"N": "12345678901234567890123456789012345678"},
            "binary_key": {"B": base64.b64encode(b"private-binary-key").decode("ascii")},
        }
        runner = ScriptedRunner([dealer_config(), count_page(1, cursor), count_page(2)])
        report = self.query(AwsQueries(self.settings(), runner=runner))
        self.assertEqual(report["count"], 3)
        encoded = next(arg for arg in runner.calls[2][0] if arg.startswith("--exclusive-start-key="))
        self.assertEqual(json.loads(encoded.split("=", 1)[1]), cursor)
        self.assertNotIn("number_key", json.dumps(report))
        self.assertNotIn("binary_key", json.dumps(report))

    def test_error_on_later_page_never_confirms_an_earlier_partial_count(self):
        for failure, error_code in (
            (CommandResult(1, "private-stdout", "AccessDenied private-stderr"), "access-denied"),
            (CommandResult(0, "private-invalid-json"), "invalid-output"),
            (CommandResult(0, "{}", stdout_truncated=True), "output-limit"),
            (CommandTimedOut("private-timeout"), "incomplete-timeout"),
            (OSError("private-path"), "unavailable"),
            (RuntimeError("private-runner"), "execution"),
        ):
            with self.subTest(error_code=error_code):
                runner = ScriptedRunner([dealer_config(), count_page(4, CURSOR), failure])
                report = self.query(AwsQueries(self.settings(), runner=runner), output_format="csv")
                self.assertEqual(report["error_code"], error_code)
                self.assertFalse(report["complete"])
                self.assertNotIn("count", report)
                self.assertNotIn("attachment", report)
                self.assertNotIn("private-", json.dumps(report))

    def test_day_bounds_use_dealer_date_and_preserve_dst_day_length(self):
        mexico = ZoneInfo("America/Mexico_City")
        for relative, expected in (("today", "2026-09-06"), ("tomorrow", "2026-09-07"), ("yesterday", "2026-09-05")):
            self.assertEqual(planning_bounds(relative, REQUESTED_AT, mexico)[0].isoformat(), expected)
        for day, lower, upper in (
            ("2026-03-08", "2026-03-08T05:00:00", "2026-03-09T03:59:59Z"),
            ("2026-11-01", "2026-11-01T04:00:00", "2026-11-02T04:59:59Z"),
        ):
            with self.subTest(day=day):
                actual = planning_bounds(day, REQUESTED_AT, ZoneInfo("America/New_York"))
                self.assertEqual(actual[1:], (lower, upper))

    def test_bounds_include_legacy_utc_forms_and_last_fractional_second_only(self):
        _, lower, upper = planning_bounds("2026-09-07", REQUESTED_AT, ZoneInfo("America/Mexico_City"))
        inside = (
            "2026-09-07T06:00:00", "2026-09-07T06:00:00+00:00", "2026-09-07T06:00:00Z",
            "2026-09-08T05:59:59", "2026-09-08T05:59:59+00:00", "2026-09-08T05:59:59.999999Z",
        )
        outside = ("2026-09-07T05:59:59.999999Z", "2026-09-08T06:00:00", "2026-09-08T06:00:00Z")
        for value in inside:
            self.assertTrue(lower <= value <= upper, value)
        for value in outside:
            self.assertFalse(lower <= value <= upper, value)


if __name__ == "__main__":
    unittest.main()
