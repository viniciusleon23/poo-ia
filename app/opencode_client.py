"""Asynchronous client for a protected local OpenCode server."""

from __future__ import annotations

import asyncio
import inspect
import re
from collections.abc import Callable
from typing import Any

import aiohttp

from .prompt_loader import build_research_prompt


class OpenCodeError(RuntimeError):
    """A recoverable problem while using the OpenCode research worker."""


class OpenCodeDisabledError(OpenCodeError):
    """The message requires research but the worker is not enabled."""


class OpenCodeTimeoutError(OpenCodeError):
    """The complete OpenCode operation exceeded its configured limit."""


DEFAULT_CLEANUP_REQUEST_TIMEOUT_SECONDS = 5.0


class OpenCodeClient:
    """Run ephemeral research sessions and clean them up afterward."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        base_url: str,
        username: str,
        password: str,
        agent: str,
        cleanup_request_timeout_seconds: float = DEFAULT_CLEANUP_REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        if cleanup_request_timeout_seconds <= 0:
            raise ValueError("cleanup_request_timeout_seconds must be positive")
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": aiohttp.encode_basic_auth(username, password)}
        self._agent = agent
        self._active_sessions: dict[str, None] = {}
        self._cleanup_timeout = aiohttp.ClientTimeout(
            total=float(cleanup_request_timeout_seconds)
        )

    @property
    def active_session_id(self) -> str | None:
        """Return the newest live session, if one exists.

        The deployed scheduler admits one OpenCode operation at a time. Keeping
        this property deterministic also makes cancellation available without
        changing the established ``research(prompt)`` return type.
        """
        if not self._active_sessions:
            return None
        return next(reversed(self._active_sessions))

    async def research(
        self,
        prompt: str,
        *,
        instructions: str = "",
        conversation_context: str = "",
        active_repository: str | None = None,
        on_session_created: Callable[[str], object] | None = None,
    ) -> str:
        """Return text from a fresh session, preserving the original simple API.

        Optional context is formatted here so callers cannot accidentally mix
        history with the current request. ``on_session_created`` lets a
        persistent job checkpoint the remote ID before the long request begins.
        """
        session_id: str | None = None
        try:
            created = await self._post_json(
                f"{self._base_url}/session",
                {"title": "Poo-IA documentation query"},
            )
            if not isinstance(created, dict) or not isinstance(created.get("id"), str):
                raise OpenCodeError("OpenCode returned an invalid session.")
            session_id = created["id"]
            self._active_sessions[session_id] = None
            if on_session_created is not None:
                callback_result = on_session_created(session_id)
                if inspect.isawaitable(callback_result):
                    await callback_result

            request_prompt = prompt
            if instructions.strip() or conversation_context.strip() or active_repository:
                request_prompt = build_research_prompt(
                    instructions,
                    prompt,
                    conversation_context=conversation_context,
                    active_repository=active_repository,
                )

            result = await self._post_json(
                f"{self._base_url}/session/{session_id}/message",
                {
                    "agent": self._agent,
                    "parts": [{"type": "text", "text": request_prompt}],
                },
            )
            return self._extract_text(result)
        except asyncio.TimeoutError as error:
            if session_id is not None:
                await self._abort_for_cleanup(session_id)
            raise OpenCodeTimeoutError("Timed out while waiting for OpenCode.") from error
        except asyncio.CancelledError:
            if session_id is not None:
                await self._abort_for_cleanup(session_id)
            raise
        except aiohttp.ClientError as error:
            raise OpenCodeError("Could not connect to OpenCode.") from error
        finally:
            if session_id is not None:
                try:
                    await self._delete_session(session_id, strict=True)
                finally:
                    self._active_sessions.pop(session_id, None)

    async def abort(self, session_id: str) -> None:
        """Ask OpenCode to abort an active session without exposing response details."""
        try:
            await self._post_json(
                f"{self._base_url}/session/{session_id}/abort",
                {},
                timeout=self._cleanup_timeout,
            )
        except (OpenCodeError, asyncio.TimeoutError, aiohttp.ClientError):
            return

    async def abort_active(self) -> str | None:
        """Abort the newest active operation and return its session ID."""
        session_id = self.active_session_id
        if session_id is not None:
            await self.abort(session_id)
        return session_id

    async def cleanup_session(self, session_id: str) -> None:
        """Abort and remove a checkpointed session, or report cleanup failure.

        The durable checkpoint must not be cleared until this method returns.
        Abort remains best-effort because a missing/already-finished session is
        expected during recovery; deletion is the authoritative cleanup step.
        """
        if not session_id.strip():
            return
        try:
            await self.abort(session_id)
        finally:
            try:
                await self._delete_session(session_id, strict=True)
            finally:
                self._active_sessions.pop(session_id, None)

    async def _abort_for_cleanup(self, session_id: str) -> None:
        """Finish the abort request even when the caller is being cancelled."""
        task = asyncio.create_task(self.abort(session_id))
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                # Preserve abort-before-delete even if shutdown sends another
                # cancellation while the cleanup request is in flight.
                continue
        await task

    async def _post_json(
        self,
        url: str,
        payload: dict[str, object],
        *,
        timeout: aiohttp.ClientTimeout | None = None,
    ) -> object:
        request_options: dict[str, object] = {
            "json": payload,
            "headers": self._headers,
        }
        if timeout is not None:
            request_options["timeout"] = timeout
        try:
            async with self._session.post(url, **request_options) as response:
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

    async def _delete_session(self, session_id: str, *, strict: bool = False) -> None:
        try:
            async with self._session.delete(
                f"{self._base_url}/session/{session_id}",
                headers=self._headers,
                timeout=self._cleanup_timeout,
            ) as response:
                if response.status == 404:
                    return
                if response.status >= 400:
                    if strict:
                        raise OpenCodeError(
                            f"Could not delete OpenCode session: HTTP {response.status}."
                        )
                    return
        except OpenCodeError:
            raise
        except (asyncio.TimeoutError, aiohttp.ClientError) as error:
            if strict:
                raise OpenCodeError("Could not delete OpenCode session.") from error

    @staticmethod
    def _extract_text(result: object) -> str:
        if not isinstance(result, dict):
            raise OpenCodeError("OpenCode returned an unexpected response body.")

        info = result.get("info")
        if isinstance(info, dict):
            error = info.get("error")
            if error is not None:
                # Provider payloads may contain credentials or response bodies.
                # Preserve a bounded error type, never relay the raw payload.
                name = error.get("name") if isinstance(error, dict) else None
                kind = (
                    f" ({name})"
                    if isinstance(name, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name)
                    else ""
                )
                raise OpenCodeError(
                    f"OpenCode reportó un error{kind}; la investigación no se completó."
                )
            if info.get("finish") == "length":
                raise OpenCodeError(
                    "OpenCode devolvió una respuesta incompleta por límite de generación."
                )
            if info.get("finish") == "error":
                raise OpenCodeError("OpenCode terminó con error; la investigación no se completó.")

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
