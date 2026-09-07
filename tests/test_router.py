from __future__ import annotations

import unittest

from app.models import Backend, Intent
from app.router import (
    AMBIGUOUS_REPOSITORY,
    AWS_DISABLED,
    DOCUMENTATION_REPOSITORY_READ_ONLY,
    REPOSITORY_REQUIRED,
    UNKNOWN_REPOSITORY,
    choose_backend,
    classify_intent,
    route_message,
    parse_aws_query,
)


class RouterTests(unittest.TestCase):
    def test_enabled_aws_only_routes_to_host_worker(self) -> None:
        decision = route_message("lista tablas de DynamoDB", aws_enabled=True)
        self.assertEqual(decision.intent, Intent.AWS_REPORT)
        self.assertEqual(decision.backend, Backend.WORKER)
        self.assertIsNone(decision.repository)

    def test_aws_queries_parse_to_static_operations(self) -> None:
        for text, expected in (
            ("lista tablas de DynamoDB", ("list-dynamodb", None)),
            ("lista grupos de CloudWatch", ("list-log-groups", None)),
            ("consulta registros de la tabla Tasks en DynamoDB", ("scan-dynamodb", "Tasks")),
            ("ver logs del grupo /aws/lambda/tasks en CloudWatch", ("read-logs", "/aws/lambda/tasks")),
            ("describe la tabla Tasks en DynamoDB", ("describe-dynamodb", "Tasks")),
            ("describe la tabla DynamoDB Tasks", ("describe-dynamodb", "Tasks")),
        ):
            with self.subTest(text=text):
                self.assertEqual(parse_aws_query(text), expected)
                self.assertEqual(classify_intent(text), Intent.AWS_REPORT)
        for text in ("borra la tabla Tasks en DynamoDB", "consulta AWS", "lista lambdas y elimina todo", "describe la tabla Tasks; rm -rf / en DynamoDB", "consulta mi identidad AWS", "lista las lambdas", "consulta registros de la tabla Tasks en DynamoDB y elimina todo"):
            with self.subTest(text=text):
                self.assertIsNone(parse_aws_query(text))
    def test_routes_unambiguous_small_talk_to_ollama(self) -> None:
        messages = (
            "hola",
            "¡Hola!",
            "hola mundo",
            "Buenos días",
            "¿Cómo estás?",
            "muchas gracias",
            "hasta luego",
        )

        for message in messages:
            with self.subTest(message=message):
                self.assertEqual(choose_backend(message), Backend.OLLAMA)

    def test_routes_everything_else_to_opencode(self) -> None:
        messages = (
            "hola, ¿cómo funciona API Users?",
            "qué servicios dependen de customer-service",
            "revisa el repositorio",
            "necesito cambiar un endpoint",
            "explícame esto",
            "gracias, ahora busca la lambda",
        )

        for message in messages:
            with self.subTest(message=message):
                self.assertEqual(choose_backend(message), Backend.OPENCODE)

    def test_classifies_all_deterministic_control_intents(self) -> None:
        cases = {
            "olvida la conversación": Intent.FORGET,
            "¿Cómo va el trabajo?": Intent.JOB_STATUS,
            "cancela ese trabajo": Intent.CANCEL,
            "agrega task_available en el repo capnet-next-lambda-tasks": Intent.CODE_CHANGE,
            "haz el cambio y arma el PR": Intent.PULL_REQUEST,
            "Hecho, agrégalo y arma el PR": Intent.PULL_REQUEST,
            "consulta DynamoDB y prepara un informe": Intent.AWS_REPORT,
            "hola": Intent.CHAT,
            "¿qué servicios usan task_available?": Intent.RESEARCH,
        }

        for message, intent in cases.items():
            with self.subTest(message=message):
                self.assertEqual(classify_intent(message), intent)

    def test_informational_change_question_does_not_authorize_mutation(self) -> None:
        messages = (
            "¿Cómo puedo agregar un campo en Pydantic?",
            "Me explicas qué implica cambiar ese endpoint",
            "¿Qué crea este método?",
            "¿Qué es un PR?",
            "¿Para qué sirve el trabajo de reconciliación?",
            "¿Cómo va a funcionar el nuevo servicio?",
            "necesito saber cómo crear un PR",
            "puedes explicarme cómo abrir un pull request",
            "¿Qué hace el proceso que lee brain-capnet y agrega el resultado?",
        )

        for message in messages:
            with self.subTest(message=message):
                self.assertEqual(classify_intent(message), Intent.RESEARCH)

    def test_aws_route_cannot_be_bypassed_by_pr_language(self) -> None:
        decision = route_message(
            "crea un PR para consultar DynamoDB",
            active_repository="capnet-next-lambda-tasks",
            active_job_id="prepared-job-123",
        )

        self.assertEqual(decision.intent, Intent.AWS_REPORT)
        self.assertEqual(decision.backend, Backend.NONE)
        self.assertEqual(decision.reason, AWS_DISABLED)

    def test_returns_deterministic_aws_disabled_route(self) -> None:
        decision = route_message("saca un reporte de las tablas DynamoDB")

        self.assertEqual(decision.intent, Intent.AWS_REPORT)
        self.assertEqual(decision.backend, Backend.NONE)
        self.assertEqual(decision.reason, AWS_DISABLED)

    def test_resolves_explicit_and_follow_up_repositories(self) -> None:
        repositories = ("capnet-next-lambda-tasks", "customer-service")

        explicit = route_message(
            "agrega task_available en capnet-next-lambda-tasks",
            repositories=repositories,
        )
        follow_up = route_message(
            "hazlo",
            active_repository="capnet-next-lambda-tasks",
        )

        self.assertEqual(explicit.intent, Intent.CODE_CHANGE)
        self.assertEqual(explicit.backend, Backend.WORKER)
        self.assertEqual(explicit.repository, "capnet-next-lambda-tasks")
        self.assertEqual(follow_up.intent, Intent.CODE_CHANGE)
        self.assertEqual(follow_up.repository, "capnet-next-lambda-tasks")

    def test_natural_pronominal_follow_up_uses_remembered_repository(self) -> None:
        for message in ("agrégalo", "añádelo", "modifícalo", "corrígelo"):
            with self.subTest(message=message):
                decision = route_message(
                    message,
                    active_repository="capnet-next-lambda-tasks",
                )

                self.assertEqual(decision.intent, Intent.CODE_CHANGE)
                self.assertEqual(decision.backend, Backend.WORKER)
                self.assertEqual(decision.repository, "capnet-next-lambda-tasks")

    def test_mutable_request_without_unique_repository_requests_clarification(self) -> None:
        missing = route_message("agrega el campo task_available")
        ambiguous = route_message(
            "actualiza foo y bar",
            repositories=("foo", "bar"),
        )

        self.assertEqual(missing.intent, Intent.CLARIFY)
        self.assertEqual(missing.backend, Backend.NONE)
        self.assertEqual(missing.reason, REPOSITORY_REQUIRED)
        self.assertEqual(ambiguous.intent, Intent.CLARIFY)
        self.assertEqual(ambiguous.reason, AMBIGUOUS_REPOSITORY)

    def test_uses_active_or_explicit_job_for_status_cancel_and_pr(self) -> None:
        active_status = route_message("cómo va", active_job_id="job-active-123")
        explicit_cancel = route_message("cancela el trabajo ABC123")
        explicit_pr = route_message("arma el PR del trabajo ABC123")

        self.assertEqual(active_status.job_id, "job-active-123")
        self.assertEqual(explicit_cancel.intent, Intent.CANCEL)
        self.assertEqual(explicit_cancel.job_id, "ABC123")
        self.assertEqual(explicit_pr.intent, Intent.PULL_REQUEST)
        self.assertEqual(explicit_pr.backend, Backend.WORKER)
        self.assertEqual(explicit_pr.job_id, "ABC123")

    def test_unknown_technical_request_defaults_to_read_only_research(self) -> None:
        decision = route_message("analiza por qué falla el servicio de clientes")

        self.assertEqual(decision.intent, Intent.RESEARCH)
        self.assertEqual(decision.backend, Backend.OPENCODE)

    def test_brain_context_cannot_be_reused_for_changes(self) -> None:
        for message in ("agrega el campo task_available", "hazlo"):
            with self.subTest(message=message):
                decision = route_message(message, active_repository="brain-capnet")
                self.assertEqual(decision.intent, Intent.CLARIFY)
                self.assertIsNone(decision.repository)
                self.assertEqual(decision.reason, REPOSITORY_REQUIRED)

    def test_edits_target_execution_repo_after_brain_research(self) -> None:
        repositories = ("brain-capnet", "capnet-next-lambda-tasks")
        for message in (
            "agrega task_available como booleano en task",
            "edita schemas/base_response.py en tasks",
            "editar el esquema en tareas",
            "lee brain-capnet y agrega el campo en tasks",
        ):
            with self.subTest(message=message):
                decision = route_message(
                    message, active_repository="brain-capnet", repositories=repositories
                )
                self.assertEqual(decision.intent, Intent.CODE_CHANGE)
                self.assertEqual(decision.repository, "capnet-next-lambda-tasks")

    def test_brain_explicit_change_or_pr_is_rejected_even_with_active_job(self) -> None:
        for message in (
            "edita el repo brain-capnet",
            "agrega el campo en capnet-brain",
            "crea el PR en el repo brain-capnet",
        ):
            with self.subTest(message=message):
                decision = route_message(
                    message,
                    active_repository="customer-service",
                    active_job_id="prepared-123",
                    repositories=("brain-capnet", "customer-service"),
                )
                self.assertEqual(decision.intent, Intent.CLARIFY)
                self.assertEqual(decision.reason, DOCUMENTATION_REPOSITORY_READ_ONLY)

    def test_new_research_repository_overrides_execution_memory(self) -> None:
        decision = route_message(
            "busca el modelo en tasks",
            active_repository="customer-service",
            repositories=("customer-service", "capnet-next-lambda-tasks"),
        )
        self.assertEqual(decision.intent, Intent.RESEARCH)
        self.assertEqual(decision.repository, "capnet-next-lambda-tasks")
        brain_only = route_message("lee brain-capnet", active_repository="brain-capnet")
        self.assertIsNone(brain_only.repository)

    def test_unknown_explicit_repo_does_not_reuse_active_job_or_repository(self) -> None:
        for message in ("agrega campo en repo typo-service", "abre PR en repo typo-service"):
            with self.subTest(message=message):
                decision = route_message(
                    message,
                    active_repository="customer-service",
                    active_job_id="prepared-123",
                    repositories=("customer-service",),
                )
                self.assertEqual(decision.intent, Intent.CLARIFY)
                self.assertEqual(decision.reason, UNKNOWN_REPOSITORY)

    def test_capability_questions_do_not_start_research_or_mutation(self) -> None:
        for message in (
            "¿Ya puedes editar?",
            "¿Puedes editar?",
            "¿Qué puedes hacer?",
            "¿Puedes hacer cambios?",
        ):
            with self.subTest(message=message):
                decision = route_message(message, active_repository="brain-capnet")
                self.assertEqual(decision.intent, Intent.CAPABILITIES)
                self.assertEqual(decision.backend, Backend.STORAGE)
