from __future__ import annotations

import unittest

from app.business_queries import parse_business_query
from app.models import Backend, Intent
from app.router import classify_intent, route_message


REAL_REQUEST = (
    "Puedes contar cuántas tareas tengo para el día de mañana en este dealer\n"
    "COMAZDCALC2\nConsidera las tareas de asesor,técnico, mantenimiento .. etc"
)


class BusinessQueryParserTests(unittest.TestCase):
    def test_real_request_is_an_inclusive_planned_task_count(self) -> None:
        parsed = parse_business_query(REAL_REQUEST)
        self.assertIsNone(parsed.clarification)
        self.assertEqual(parsed.query.to_payload(123.5), {
            "dealer_id": "COMAZDCALC2", "day": "tomorrow", "requested_at": 123.5,
        })
        route = route_message(REAL_REQUEST, aws_enabled=True, active_repository="brain-capnet")
        self.assertEqual((route.intent, route.backend), (Intent.AWS_REPORT, Backend.WORKER))
        self.assertIsNone(route.repository)

    def test_count_and_summary_variants_preserve_dealer_and_day(self) -> None:
        for text, dealer, day in (
            ("¿Cuántas tareas hay HOY para el dealer MiDealer-2?", "MiDealer-2", "today"),
            ("Por favor, cuenta las tareas planeadas para ayer del dealer: ABC_12", "ABC_12", "yesterday"),
            ("Dame un resumen de todas las tareas del dealer `ABC123` para 2026-09-07", "ABC123", "2026-09-07"),
            ("Número de tareas para mañana en la agencia ABC123", "ABC123", "tomorrow"),
            ("Necesito saber el total de tareas mañana del distribuidor ABC123", "ABC123", "tomorrow"),
            ("Resume las tareas de mañana del dealer ABC123, incluyendo todos los tipos", "ABC123", "tomorrow"),
            ("Cuántas tareas hay mañana en dealer ABC123 en csv", "ABC123", "tomorrow"),
            ("Cuántas tareas hay mañana en dealer ABC123 y dámelo en formato CSV por favor", "ABC123", "tomorrow"),
            ("Cuenta tareas mañana en dealer IDABC123", "IDABC123", "tomorrow"),
            ("Cuenta tareas mañana en dealer codigo2", "codigo2", "tomorrow"),
            ("Usando la documentación, cuenta tareas mañana del dealer ABC123", "ABC123", "tomorrow"),
            ("Cuenta tareas mañana del dealer ABC123 en DynamoDB", "ABC123", "tomorrow"),
            ("Cuenta tareas mañana del dealer ABC123 en AWS", "ABC123", "tomorrow"),
        ):
            with self.subTest(text=text):
                parsed = parse_business_query(text)
                self.assertIsNotNone(parsed)
                self.assertIsNone(parsed.clarification)
                self.assertEqual((parsed.query.dealer_id, parsed.query.day), (dealer, day))

    def test_documentation_and_internal_job_questions_are_not_data_queries(self) -> None:
        for text in (
            "Explica cómo se cuentan las tareas del dealer ABC123 para mañana",
            "¿Dónde está el código para contar tareas por dealer y fecha?",
            "Cómo funciona el conteo de tareas para mañana del dealer ABC123",
            "Resume la documentación de las tareas y los dealers",
            "Cuántas tareas tiene el scheduler del bot",
            "Cómo va el trabajo ABC123",
            "Estado del trabajo ABC123",
        ):
            with self.subTest(text=text):
                self.assertIsNone(parse_business_query(text))
                self.assertNotEqual(classify_intent(text), Intent.AWS_REPORT)

    def test_explicit_repository_changes_do_not_execute_a_business_count(self) -> None:
        for text in (
            "Agrega un endpoint para contar tareas del dealer ABC123 para mañana en capnet-next-lambda-tasks",
            "Modifica el conteo de tareas de mañana del dealer ABC123 en capnet-next-lambda-tasks",
        ):
            self.assertIsNone(parse_business_query(text))
            route = route_message(text, repositories=("capnet-next-lambda-tasks",), aws_enabled=True)
            self.assertEqual(route.intent, Intent.CODE_CHANGE)

    def test_unsupported_filters_are_recognized_for_clarification_without_a_plan(self) -> None:
        for text, marker in (
            ("Cuenta tareas pendientes mañana del dealer ABC123", "estado"),
            ("Cuenta tareas con estado abierto mañana del dealer ABC123", "estado"),
            ("Cuenta mis tareas mañana del dealer ABC123", "usuario"),
            ("Cuenta tareas asignadas al usuario Juan mañana del dealer ABC123", "usuario"),
            ("Cuenta tareas de asesor y técnico mañana del dealer ABC123", "tipo"),
            ("Cuenta solo tareas de mantenimiento mañana del dealer ABC123", "tipo"),
            ("Cuenta todas las tareas excepto mantenimiento mañana del dealer ABC123", "tipo"),
            ("Cuenta tareas de prioridad alta mañana del dealer ABC123", "filtro"),
            ("Cuenta tareas creadas mañana del dealer ABC123", "planeadas"),
        ):
            with self.subTest(text=text):
                parsed = parse_business_query(text)
                self.assertIsNotNone(parsed)
                self.assertIsNone(parsed.query)
                self.assertIn(marker, parsed.clarification)
                self.assertEqual(classify_intent(text), Intent.AWS_REPORT)

    def test_missing_or_ambiguous_dealer_is_not_inferred(self) -> None:
        for text in (
            "Cuántas tareas hay mañana",
            "Cuántas tareas hay para mañana en este dealer",
            "Cuenta tareas mañana en dealer ABC123 y dealer DEF456",
            "Cuenta tareas mañana en dealer ../ABC123",
        ):
            parsed = parse_business_query(text)
            self.assertIsNone(parsed.query)
            self.assertIn("dealer", parsed.clarification)

    def test_unsupported_and_ambiguous_dates_require_one_supported_day(self) -> None:
        for text in (
            "Cuenta tareas del dealer ABC123",
            "Cuenta tareas mañana y ayer del dealer ABC123",
            "Cuenta tareas pasado mañana del dealer ABC123",
            "Cuenta tareas el próximo lunes del dealer ABC123",
            "Cuenta tareas del 2026-09-07 al 2026-09-08 del dealer ABC123",
            "Cuenta tareas mañana a las 09:00 del dealer ABC123",
            "Cuenta tareas mañana por la tarde del dealer ABC123",
            "Cuenta tareas 07/09/2026 del dealer ABC123",
            "Cuenta tareas 2026-02-30 del dealer ABC123",
            "Cuenta tareas mañana 2026-09-07 del dealer ABC123",
        ):
            with self.subTest(text=text):
                parsed = parse_business_query(text)
                self.assertIsNone(parsed.query)
                self.assertIn("día", parsed.clarification)

    def test_unrecognized_constraints_and_mixed_mutations_never_produce_a_plan(self) -> None:
        for text in (
            "Cuenta tareas mañana del dealer ABC123 con task_available false",
            "Cuenta tareas mañana del dealer ABC123 con importe mayor a 500",
            "Cuenta tareas mañana del dealer ABC123; borra las completadas",
            "Cuenta tareas mañana del dealer ABC123 usa tabla Users",
        ):
            parsed = parse_business_query(text)
            self.assertIsNotNone(parsed)
            self.assertIsNone(parsed.query)
            self.assertTrue(parsed.clarification)

    def test_business_query_respects_disabled_aws_without_falling_back_to_docs(self) -> None:
        route = route_message(REAL_REQUEST, aws_enabled=False)
        self.assertEqual((route.intent, route.backend), (Intent.AWS_REPORT, Backend.NONE))

    def test_operational_status_and_listing_are_not_internal_job_controls_or_docs(self) -> None:
        for text in (
            "consulta el estado de las tareas del dealer ABC123 para mañana",
            "muestra las tareas del dealer ABC123 para mañana",
            "lista tareas del dealer ABC123 para mañana",
        ):
            parsed = parse_business_query(text)
            self.assertIsNone(parsed.query)
            self.assertTrue(parsed.clarification)
            self.assertEqual(classify_intent(text), Intent.AWS_REPORT)
        self.assertEqual(classify_intent("Cómo consultar las tareas del dealer en el código"), Intent.RESEARCH)

    def test_invalid_reference_timestamp_is_a_deterministic_value_error(self) -> None:
        query = parse_business_query(REAL_REQUEST).query
        for timestamp in (True, "123", float("inf"), float("nan"), 10 ** 400):
            with self.subTest(timestamp_type=type(timestamp).__name__):
                with self.assertRaises(ValueError):
                    query.to_payload(timestamp)
