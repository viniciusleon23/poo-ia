from __future__ import annotations

import asyncio
import unittest
from collections.abc import Mapping

from app.opencode_client import OpenCodeClient, OpenCodeError, OpenCodeTimeoutError


class FakeResponse:
    def __init__(self, status: int, data: object) -> None:
        self.status = status
        self._data = data

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        return None

    async def json(self, **_kwargs: object) -> object:
        if isinstance(self._data, Exception):
            raise self._data
        return self._data


class BlockingResponse(FakeResponse):
    def __init__(self, started: asyncio.Event) -> None:
        super().__init__(200, {})
        self._started = started

    async def json(self, **_kwargs: object) -> object:
        self._started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class FakeSession:
    def __init__(self, responses: list[FakeResponse | Exception]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, Mapping[str, object]]] = []

    def _request(self, method: str, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def post(self, url: str, **kwargs: object) -> FakeResponse:
        return self._request("POST", url, **kwargs)

    def delete(self, url: str, **kwargs: object) -> FakeResponse:
        return self._request("DELETE", url, **kwargs)


class OpenCodeClientTests(unittest.IsolatedAsyncioTestCase):
    def make_client(self, session: FakeSession) -> OpenCodeClient:
        return OpenCodeClient(
            session,
            base_url="http://127.0.0.1:4096/",
            username="opencode",
            password="secret",
            agent="capnet-research",
        )

    async def test_creates_session_returns_text_and_always_deletes(self) -> None:
        session = FakeSession(
            [
                FakeResponse(200, {"id": "session-1"}),
                FakeResponse(
                    200,
                    {
                        "info": {"id": "message-1"},
                        "parts": [
                            {"type": "reasoning", "text": "internal"},
                            {"type": "text", "text": "  Respuesta "},
                            {"type": "text", "text": "con fuentes.  "},
                        ],
                    },
                ),
                FakeResponse(200, True),
            ]
        )

        result = await self.make_client(session).research("consulta")

        self.assertEqual(result, "Respuesta\n\ncon fuentes.")
        self.assertEqual([call[0] for call in session.calls], ["POST", "POST", "DELETE"])
        self.assertEqual(session.calls[0][1], "http://127.0.0.1:4096/session")
        self.assertEqual(
            session.calls[1][2]["json"],
            {
                "agent": "capnet-research",
                "parts": [{"type": "text", "text": "consulta"}],
            },
        )
        headers = session.calls[0][2]["headers"]
        self.assertEqual(headers["Authorization"], "Basic b3BlbmNvZGU6c2VjcmV0")

    async def test_deletes_session_when_message_fails(self) -> None:
        session = FakeSession(
            [
                FakeResponse(200, {"id": "session-2"}),
                FakeResponse(503, {}),
                FakeResponse(200, True),
            ]
        )

        with self.assertRaisesRegex(OpenCodeError, "HTTP 503"):
            await self.make_client(session).research("consulta")

        self.assertEqual(session.calls[-1][0], "DELETE")
        self.assertTrue(session.calls[-1][1].endswith("/session/session-2"))

    async def test_rejects_invalid_or_empty_responses(self) -> None:
        cases = (
            FakeResponse(200, ["not-an-object"]),
            FakeResponse(200, {"info": {}, "parts": []}),
            FakeResponse(200, {"parts": [{"type": "text", "text": "  "}]}),
        )

        for message_response in cases:
            with self.subTest(data=message_response._data):
                session = FakeSession(
                    [
                        FakeResponse(200, {"id": "session-3"}),
                        message_response,
                        FakeResponse(200, True),
                    ]
                )
                with self.assertRaises(OpenCodeError):
                    await self.make_client(session).research("consulta")

    async def test_rejects_error_metadata_even_with_partial_text_and_cleans_session(self) -> None:
        session = FakeSession(
            [
                FakeResponse(200, {"id": "session-partial"}),
                FakeResponse(200, {
                    "info": {"error": {
                        "name": "APIError",
                        "data": {"message": "sensitive provider payload", "statusCode": 429},
                    }},
                    "parts": [{"type": "text", "text": "Ya identifiqué un archivo."}],
                }),
                FakeResponse(200, True),
            ]
        )

        with self.assertRaises(OpenCodeError) as raised:
            await self.make_client(session).research("consulta")

        self.assertIn("APIError", str(raised.exception))
        self.assertNotIn("sensitive provider payload", str(raised.exception))
        self.assertEqual(session.calls[-1][0], "DELETE")

    def test_rejects_confirmed_incomplete_finish_reasons(self) -> None:
        for finish in ("length", "error"):
            with self.subTest(finish=finish):
                with self.assertRaisesRegex(OpenCodeError, "incompleta|error"):
                    OpenCodeClient._extract_text({
                        "info": {"finish": finish},
                        "parts": [{"type": "text", "text": "Texto parcial."}],
                    })

    def test_unknown_finish_does_not_invent_an_error(self) -> None:
        for finish in (None, "stop", "unknown", "provider-specific"):
            with self.subTest(finish=finish):
                self.assertEqual(OpenCodeClient._extract_text({
                    "info": {"finish": finish, "error": None},
                    "parts": [{"type": "text", "text": "Respuesta completa."}],
                }), "Respuesta completa.")

    def test_error_object_is_rejected_without_leaking_arbitrary_fields(self) -> None:
        for error in ({}, {"name": "unsafe\nprovider-token"}, {"data": {"secret": "token"}}):
            with self.subTest(error=error):
                with self.assertRaises(OpenCodeError) as raised:
                    OpenCodeClient._extract_text({
                        "info": {"error": error},
                        "parts": [{"type": "text", "text": "Texto parcial."}],
                    })
                self.assertNotIn("provider-token", str(raised.exception))

    async def test_maps_timeout_to_safe_error(self) -> None:
        session = FakeSession([asyncio.TimeoutError()])

        with self.assertRaisesRegex(OpenCodeTimeoutError, "Timed out"):
            await self.make_client(session).research("consulta")

    async def test_timeout_after_creation_aborts_before_deleting(self) -> None:
        session = FakeSession(
            [
                FakeResponse(200, {"id": "session-timeout"}),
                asyncio.TimeoutError(),
                FakeResponse(200, True),
                FakeResponse(200, True),
            ]
        )

        with self.assertRaises(OpenCodeTimeoutError):
            await self.make_client(session).research("consulta")

        self.assertEqual(
            [(method, url.rsplit("/", 1)[-1]) for method, url, _ in session.calls],
            [
                ("POST", "session"),
                ("POST", "message"),
                ("POST", "abort"),
                ("DELETE", "session-timeout"),
            ],
        )

    async def test_cancelled_research_aborts_before_delete_and_clears_active_id(self) -> None:
        started = asyncio.Event()
        session = FakeSession(
            [
                FakeResponse(200, {"id": "session-cancel"}),
                BlockingResponse(started),
                FakeResponse(200, True),
                FakeResponse(200, True),
            ]
        )
        client = self.make_client(session)
        observed_sessions: list[str] = []
        task = asyncio.create_task(
            client.research("consulta", on_session_created=observed_sessions.append)
        )
        await asyncio.wait_for(started.wait(), timeout=1)

        self.assertEqual(client.active_session_id, "session-cancel")
        self.assertEqual(observed_sessions, ["session-cancel"])
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(client.active_session_id, None)
        self.assertEqual([call[0] for call in session.calls], ["POST", "POST", "POST", "DELETE"])
        self.assertTrue(session.calls[2][1].endswith("/session/session-cancel/abort"))
        self.assertTrue(session.calls[3][1].endswith("/session/session-cancel"))

    async def test_formats_optional_context_without_breaking_simple_research_api(self) -> None:
        session = FakeSession(
            [
                FakeResponse(200, {"id": "session-context"}),
                FakeResponse(200, {"parts": [{"type": "text", "text": "resultado"}]}),
                FakeResponse(200, True),
            ]
        )

        result = await self.make_client(session).research(
            "¿Dónde está task_available?",
            instructions="Cita rutas.",
            conversation_context="Usuario: revisa el schema",
            active_repository="capnet-next-lambda-tasks",
        )

        self.assertEqual(result, "resultado")
        sent_prompt = session.calls[1][2]["json"]["parts"][0]["text"]
        self.assertIn("Cita rutas.", sent_prompt)
        self.assertIn("Usuario: revisa el schema", sent_prompt)
        self.assertIn("capnet-next-lambda-tasks", sent_prompt)
        self.assertIn("¿Dónde está task_available?", sent_prompt)

    async def test_recovery_cleanup_reports_delete_failure_instead_of_losing_session(self) -> None:
        session = FakeSession(
            [
                FakeResponse(200, True),
                FakeResponse(503, {}),
            ]
        )

        with self.assertRaisesRegex(OpenCodeError, "delete.*HTTP 503"):
            await self.make_client(session).cleanup_session("orphan-session")

        self.assertEqual([call[0] for call in session.calls], ["POST", "DELETE"])

    async def test_recovery_cleanup_accepts_already_deleted_session(self) -> None:
        session = FakeSession(
            [
                FakeResponse(404, {}),
                FakeResponse(404, {}),
            ]
        )

        await self.make_client(session).cleanup_session("orphan-session")

        self.assertEqual([call[0] for call in session.calls], ["POST", "DELETE"])
        self.assertEqual(session.calls[0][2]["timeout"].total, 5.0)
        self.assertEqual(session.calls[1][2]["timeout"].total, 5.0)

    def test_rejects_nonpositive_cleanup_request_timeout(self) -> None:
        with self.assertRaises(ValueError):
            OpenCodeClient(
                FakeSession([]),
                base_url="http://127.0.0.1:4096",
                username="opencode",
                password="secret",
                agent="capnet-research",
                cleanup_request_timeout_seconds=0,
            )
