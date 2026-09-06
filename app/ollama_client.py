"""Small asynchronous client for Ollama's generate endpoint."""

from __future__ import annotations

import asyncio

import aiohttp


class OllamaError(RuntimeError):
    """A recoverable problem while generating an Ollama response."""


class OllamaClient:
    """Generate complete responses using one shared aiohttp session."""

    def __init__(self, session: aiohttp.ClientSession, *, base_url: str, model: str) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._model = model

    async def generate(self, prompt: str) -> str:
        """Return a non-empty response or raise ``OllamaError``."""
        payload = {"model": self._model, "prompt": prompt, "stream": False}
        url = f"{self._base_url}/api/generate"

        try:
            async with self._session.post(url, json=payload) as response:
                if response.status >= 400:
                    raise OllamaError(f"Ollama returned HTTP {response.status}.")
                try:
                    data = await response.json(content_type=None)
                except (aiohttp.ClientError, ValueError) as error:
                    raise OllamaError("Ollama returned an invalid response.") from error
        except asyncio.TimeoutError as error:
            raise OllamaError("Timed out while waiting for Ollama.") from error
        except aiohttp.ClientError as error:
            raise OllamaError("Could not connect to Ollama.") from error

        if not isinstance(data, dict):
            raise OllamaError("Ollama returned an unexpected response body.")

        generated_text = data.get("response")
        if not isinstance(generated_text, str) or not generated_text.strip():
            raise OllamaError("Ollama returned an empty response.")
        return generated_text.strip()
