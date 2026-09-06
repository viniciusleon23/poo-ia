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

    async def test_maps_timeout_to_safe_error(self) -> None:
        session = FakeSession([asyncio.TimeoutError()])

        with self.assertRaisesRegex(OpenCodeTimeoutError, "Timed out"):
            await self.make_client(session).research("consulta")
