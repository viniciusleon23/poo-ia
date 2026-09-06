from __future__ import annotations

import unittest

from app.ollama_client import OllamaClient, OllamaError


class FakeResponse:
    def __init__(self, status: int, data: object) -> None:
        self.status = status
        self._data = data

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        return None

    async def json(self, **_kwargs: object) -> object:
        return self._data


class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.url: str | None = None
        self.payload: dict[str, object] | None = None

    def post(self, url: str, *, json: dict[str, object]) -> FakeResponse:
        self.url = url
        self.payload = json
        return self.response


class OllamaClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_sends_the_model_and_returns_text(self) -> None:
        session = FakeSession(FakeResponse(200, {"response": "  Hola desde Ollama  "}))
        client = OllamaClient(session, base_url="http://127.0.0.1:11434/", model="modelo")

        result = await client.generate("prompt de prueba")

        self.assertEqual(result, "Hola desde Ollama")
        self.assertEqual(session.url, "http://127.0.0.1:11434/api/generate")
        self.assertEqual(
            session.payload,
            {"model": "modelo", "prompt": "prompt de prueba", "stream": False},
        )

    async def test_raises_a_safe_error_for_bad_response_status(self) -> None:
        session = FakeSession(FakeResponse(503, {}))
        client = OllamaClient(session, base_url="http://127.0.0.1:11434", model="modelo")

        with self.assertRaisesRegex(OllamaError, "HTTP 503"):
            await client.generate("prompt")
