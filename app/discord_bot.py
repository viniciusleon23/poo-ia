"""Discord event handling for Poo-IA."""

from __future__ import annotations

import asyncio
import logging

import aiohttp
import discord

from .config import Settings
from .ollama_client import OllamaClient, OllamaError
from .opencode_client import (
    OpenCodeClient,
    OpenCodeDisabledError,
    OpenCodeError,
    OpenCodeTimeoutError,
)
from .prompt_loader import build_prompt, load_prompt_context
from .router import Backend, choose_backend
from .text import split_for_discord


LOGGER = logging.getLogger(__name__)
OLLAMA_UNAVAILABLE_MESSAGE = "No pude obtener una respuesta de Ollama. Inténtalo de nuevo en un momento."
OPENCODE_DISABLED_MESSAGE = "El cerebro documental todavía no está habilitado. Inténtalo más tarde."
OPENCODE_TIMEOUT_MESSAGE = "La consulta documental tardó demasiado. Inténtalo de nuevo en un momento."
OPENCODE_UNAVAILABLE_MESSAGE = "No pude consultar la documentación en este momento. Inténtalo más tarde."


def should_respond_to(message: discord.Message, settings: Settings) -> bool:
    """Apply all routing guards before doing any work for a Discord message."""
    if message.author.bot:
        return False
    if message.channel.id != settings.discord_channel_id:
        return False
    if settings.allowed_user_id is not None and message.author.id != settings.allowed_user_id:
        return False
    return bool(message.content and message.content.strip())


async def generate_response(
    content: str,
    settings: Settings,
    *,
    ollama: OllamaClient,
    opencode: OpenCodeClient | None,
    opencode_semaphore: asyncio.Semaphore,
) -> str:
    """Generate through the selected backend without changing the user's text."""
    if choose_backend(content) is Backend.OLLAMA:
        instructions = load_prompt_context(settings.content_root)
        prompt = build_prompt(instructions, content)
        return await ollama.generate(prompt)

    if not settings.opencode_enabled or opencode is None:
        raise OpenCodeDisabledError("OpenCode is disabled.")

    try:
        async with asyncio.timeout(settings.opencode_timeout_seconds):
            async with opencode_semaphore:
                return await opencode.research(content)
    except TimeoutError as error:
        raise OpenCodeTimeoutError("Timed out while waiting for OpenCode.") from error


class PooIAClient(discord.Client):
    """A Discord client that forwards allowed messages to local Ollama."""

    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.settings = settings
        self._ollama_session: aiohttp.ClientSession | None = None
        self._opencode_session: aiohttp.ClientSession | None = None
        self._ollama: OllamaClient | None = None
        self._opencode: OpenCodeClient | None = None
        self._opencode_semaphore = asyncio.Semaphore(settings.opencode_max_concurrent)

    async def setup_hook(self) -> None:
        ollama_timeout = aiohttp.ClientTimeout(total=self.settings.ollama_timeout_seconds)
        self._ollama_session = aiohttp.ClientSession(timeout=ollama_timeout)
        self._ollama = OllamaClient(
            self._ollama_session,
            base_url=self.settings.ollama_base_url,
            model=self.settings.ollama_model,
        )

        if self.settings.opencode_enabled:
            password = self.settings.opencode_server_password
            if password is None:
                raise RuntimeError("OpenCode was enabled without a server password.")
            opencode_timeout = aiohttp.ClientTimeout(
                total=self.settings.opencode_timeout_seconds
            )
            self._opencode_session = aiohttp.ClientSession(timeout=opencode_timeout)
            self._opencode = OpenCodeClient(
                self._opencode_session,
                base_url=self.settings.opencode_base_url,
                username=self.settings.opencode_server_username,
                password=password,
                agent=self.settings.opencode_agent,
            )

    async def close(self) -> None:
        for session in (self._ollama_session, self._opencode_session):
            if session is not None and not session.closed:
                await session.close()
        await super().close()

    async def on_ready(self) -> None:
        LOGGER.info(
            "Connected as %s; listening only to channel %s.",
            self.user,
            self.settings.discord_channel_id,
        )

    async def on_message(self, message: discord.Message) -> None:
        if not should_respond_to(message, self.settings):
            return

        ollama = self._ollama
        if ollama is None:
            LOGGER.error("Ollama client was not initialized before receiving a message.")
            return

        async with message.channel.typing():
            try:
                generated_text = await generate_response(
                    message.content,
                    self.settings,
                    ollama=ollama,
                    opencode=self._opencode,
                    opencode_semaphore=self._opencode_semaphore,
                )
            except OllamaError as error:
                LOGGER.warning("Ollama generation failed: %s", error)
                await message.channel.send(OLLAMA_UNAVAILABLE_MESSAGE)
                return
            except OpenCodeDisabledError:
                await message.channel.send(OPENCODE_DISABLED_MESSAGE)
                return
            except OpenCodeTimeoutError as error:
                LOGGER.warning("OpenCode research timed out: %s", error)
                await message.channel.send(OPENCODE_TIMEOUT_MESSAGE)
                return
            except OpenCodeError as error:
                LOGGER.warning("OpenCode research failed: %s", error)
                await message.channel.send(OPENCODE_UNAVAILABLE_MESSAGE)
                return

        for chunk in split_for_discord(generated_text):
            await message.channel.send(chunk)
