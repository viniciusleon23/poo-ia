"""Asynchronous client for a protected local OpenCode server."""

from __future__ import annotations

import asyncio
from typing import Any

import aiohttp


class OpenCodeError(RuntimeError):
    """A recoverable problem while using the OpenCode research worker."""


class OpenCodeDisabledError(OpenCodeError):
    """The message requires research but the worker is not enabled."""


class OpenCodeTimeoutError(OpenCodeError):
    """The complete OpenCode operation exceeded its configured limit."""


class OpenCodeClient:
    """Run one stateless research session and clean it up afterward."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        base_url: str,
        username: str,
        password: str,
        agent: str,
    ) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": aiohttp.encode_basic_auth(username, password)}
        self._agent = agent

    async def research(self, prompt: str) -> str:
        """Return the final text from a fresh OpenCode session."""
        session_id: str | None = None
        try:
            created = await self._post_json(
                f"{self._base_url}/session",
                {"title": "Poo-IA documentation query"},
            )
            if not isinstance(created, dict) or not isinstance(created.get("id"), str):
                raise OpenCodeError("OpenCode returned an invalid session.")
            session_id = created["id"]

            result = await self._post_json(
                f"{self._base_url}/session/{session_id}/message",
                {
                    "agent": self._agent,
                    "parts": [{"type": "text", "text": prompt}],
                },
            )
            return self._extract_text(result)
        except asyncio.TimeoutError as error:
            raise OpenCodeTimeoutError("Timed out while waiting for OpenCode.") from error
        except aiohttp.ClientError as error:
            raise OpenCodeError("Could not connect to OpenCode.") from error
        finally:
            if session_id is not None:
                await self._delete_session(session_id)

    async def abort(self, session_id: str) -> None:
        """Ask OpenCode to abort an active session without exposing response details."""
        try:
            await self._post_json(
                f"{self._base_url}/session/{session_id}/abort",
                {},
            )
        except OpenCodeError:
            return

    async def _post_json(self, url: str, payload: dict[str, object]) -> object:
        try:
            async with self._session.post(
                url, json=payload, headers=self._headers
            ) as response:
                if response.status >= 400:
                    raise OpenCodeError(f"OpenCode returned HTTP {response.status}.")
                try:
                    return await response.json(content_type=None)
                except (aiohttp.ClientError, ValueError) as error:
                    raise OpenCodeError("OpenCode returned an invalid response.") from error
        except asyncio.TimeoutError:
            raise
        except aiohttp.ClientError:
            raise

    async def _delete_session(self, session_id: str) -> None:
        try:
            async with self._session.delete(
                f"{self._base_url}/session/{session_id}", headers=self._headers
            ) as response:
                if response.status >= 400:
                    return
        except (asyncio.TimeoutError, aiohttp.ClientError):
            return

    @staticmethod
    def _extract_text(result: object) -> str:
        if not isinstance(result, dict):
            raise OpenCodeError("OpenCode returned an unexpected response body.")

        parts: Any = result.get("parts")
        if not isinstance(parts, list):
            raise OpenCodeError("OpenCode returned an unexpected response body.")

        text_parts = [
            part["text"].strip()
            for part in parts
            if isinstance(part, dict)
            and part.get("type") == "text"
            and isinstance(part.get("text"), str)
            and part["text"].strip()
        ]
        if not text_parts:
            raise OpenCodeError("OpenCode returned an empty response.")
        return "\n\n".join(text_parts)
