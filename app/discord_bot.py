"""Discord event handling for Poo-IA."""

from __future__ import annotations

import logging

import aiohttp
import discord

from .config import Settings
from .ollama_client import OllamaClient, OllamaError
from .prompt_loader import build_prompt, load_prompt_context
from .text import split_for_discord


LOGGER = logging.getLogger(__name__)
OLLAMA_UNAVAILABLE_MESSAGE = "No pude obtener una respuesta de Ollama. Inténtalo de nuevo en un momento."


def should_respond_to(message: discord.Message, settings: Settings) -> bool:
    """Apply all routing guards before doing any work for a Discord message."""
    if message.author.bot:
        return False
    if message.channel.id != settings.discord_channel_id:
        return False
    if settings.allowed_user_id is not None and message.author.id != settings.allowed_user_id:
        return False
    return bool(message.content and message.content.strip())


class PooIAClient(discord.Client):
    """A Discord client that forwards allowed messages to local Ollama."""

    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.settings = settings
        self._session: aiohttp.ClientSession | None = None
        self._ollama: OllamaClient | None = None

    async def setup_hook(self) -> None:
        timeout = aiohttp.ClientTimeout(total=self.settings.ollama_timeout_seconds)
        self._session = aiohttp.ClientSession(timeout=timeout)
        self._ollama = OllamaClient(
            self._session,
            base_url=self.settings.ollama_base_url,
            model=self.settings.ollama_model,
        )

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
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

        instructions = load_prompt_context(self.settings.content_root)
        prompt = build_prompt(instructions, message.content)

        async with message.channel.typing():
            try:
                generated_text = await ollama.generate(prompt)
            except OllamaError as error:
                LOGGER.warning("Ollama generation failed: %s", error)
                await message.channel.send(OLLAMA_UNAVAILABLE_MESSAGE)
                return

        for chunk in split_for_discord(generated_text):
            await message.channel.send(chunk)
