"""Discord event handling for Poo-IA."""

from __future__ import annotations

import asyncio
import logging
from io import BytesIO
from contextlib import asynccontextmanager

import aiohttp
import discord

from .config import Settings
from .instance_lock import InstanceLock
from .memory import MemoryStore
from .models import ConversationKey, InboundMessage, OutboxPart
from .ollama_client import OllamaClient, OllamaError
from .opencode_client import (
    OpenCodeClient,
    OpenCodeDisabledError,
    OpenCodeError,
    OpenCodeTimeoutError,
)
from .prompt_loader import build_prompt, load_prompt_context
from .router import Backend, choose_backend
from .orchestrator import PROCESSING_FAILED_MESSAGE, PooIAOrchestrator
from .outbox import DurableOutbox
from .storage import SQLiteStorage
from .worker_client import WorkerClient, WorkerError


LOGGER = logging.getLogger(__name__)
DISCORD_DELIVERY_BACKOFF_SECONDS = 2
VISUAL_FEEDBACK_TIMEOUT_SECONDS = 2.0
REPOSITORY_REFRESH_TIMEOUT_SECONDS = 2.0
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
    """Thin Discord adapter over the transport-neutral Poo-IA core."""

    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.settings = settings
        self._ollama_session: aiohttp.ClientSession | None = None
        self._opencode_session: aiohttp.ClientSession | None = None
        self._worker_session: aiohttp.ClientSession | None = None
        self._ollama: OllamaClient | None = None
        self._opencode: OpenCodeClient | None = None
        self._worker: WorkerClient | None = None
        self._storage: SQLiteStorage | None = None
        self._instance_lock: InstanceLock | None = None
        self._orchestrator: PooIAOrchestrator | None = None
        self._delivery_task: asyncio.Task[None] | None = None
        self._delivery_lock = asyncio.Lock()
        self._repository_refresh_lock = asyncio.Lock()
        self._next_repository_refresh = 0.0
        self._next_storage_prune = 0.0
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

        repositories: tuple[str, ...] = ()
        if self.settings.worker_enabled:
            password = self.settings.worker_server_password
            if password is None:
                raise RuntimeError("The worker was enabled without a server password.")
            worker_timeout = aiohttp.ClientTimeout(
                total=self.settings.worker_timeout_seconds
            )
            self._worker_session = aiohttp.ClientSession(timeout=worker_timeout)
            self._worker = WorkerClient(
                self._worker_session,
                base_url=self.settings.worker_base_url,
                username=self.settings.worker_server_username,
                password=password,
            )
            try:
                await self._worker.health()
                repositories = await self._worker.list_repositories()
            except WorkerError as error:
                # Conversation and documentation still work while the host worker
                # is temporarily unavailable. Its next operation will report the
                # recoverable error through the durable job result.
                LOGGER.warning("Host worker startup check failed: %s", error)

        self._instance_lock = InstanceLock.for_database(self.settings.database_path)
        try:
            self._storage = SQLiteStorage(self.settings.database_path)
        except Exception:
            self._instance_lock.close()
            self._instance_lock = None
            raise
        self._storage.prune(
            memory_retention_seconds=self.settings.memory_retention_days * 86_400,
            operational_retention_seconds=(
                self.settings.operational_retention_days * 86_400
            ),
        )
        memory = MemoryStore(
            self._storage,
            retention_days=self.settings.memory_retention_days,
            max_exchanges=self.settings.memory_max_exchanges,
            max_context_chars=self.settings.memory_max_context_chars,
        )
        outbox = DurableOutbox(self._storage)
        self._orchestrator = PooIAOrchestrator(
            storage=self._storage,
            memory=memory,
            outbox=outbox,
            ollama=self._ollama,
            content_root=self.settings.content_root,
            opencode=self._opencode,
            worker=self._worker,
            aws_enabled=self.settings.aws_enabled,
            repositories=repositories,
            worker_poll_seconds=self.settings.worker_poll_seconds,
        )
        self._next_repository_refresh = asyncio.get_running_loop().time() + 60
        self._next_storage_prune = asyncio.get_running_loop().time() + 3_600
        await self._orchestrator.start()

    async def close(self) -> None:
        delivery_task = self._delivery_task
        if delivery_task is not None:
            delivery_task.cancel()
            try:
                await delivery_task
            except asyncio.CancelledError:
                pass
            self._delivery_task = None
        if self._orchestrator is not None:
            await self._orchestrator.close()
        for session in (
            self._ollama_session,
            self._opencode_session,
            self._worker_session,
        ):
            if session is not None and not session.closed:
                await session.close()
        if self._storage is not None:
            self._storage.close()
        if self._instance_lock is not None:
            self._instance_lock.close()
            self._instance_lock = None
        await super().close()

    async def on_ready(self) -> None:
        LOGGER.info(
            "Connected as %s; listening only to channel %s and owner %s.",
            self.user,
            self.settings.discord_channel_id,
            self.settings.allowed_user_id,
        )
        if self._delivery_task is None or self._delivery_task.done():
            self._delivery_task = asyncio.create_task(
                self._delivery_loop(), name="poo-ia-discord-outbox"
            )
        await self._flush_pending()

    async def on_message(self, message: discord.Message) -> None:
        if not should_respond_to(message, self.settings):
            return

        orchestrator = self._orchestrator
        if orchestrator is None:
            LOGGER.error("Poo-IA core was not initialized before receiving a message.")
            return

        inbound = InboundMessage(
            message_id=message.id,
            channel_id=message.channel.id,
            user_id=message.author.id,
            text=message.content,
        )
        try:
            await self._show_receipt(message)
            async with self._show_typing(message.channel):
                if self._worker is not None and not orchestrator.repositories:
                    await self._bounded_repository_refresh(force=True)
                await orchestrator.handle(inbound)
        except Exception as error:
            LOGGER.exception("Failed to process Discord message %s: %s", message.id, error)
            try:
                await orchestrator.record_processing_failure(
                    inbound, safe_message=PROCESSING_FAILED_MESSAGE
                )
            except Exception as durable_error:
                # SQLite/outbox itself is unavailable. This is the only direct,
                # non-durable fallback so the owner is not left without feedback.
                LOGGER.exception(
                    "Could not persist failure for Discord message %s: %s",
                    message.id,
                    durable_error,
                )
                try:
                    await message.channel.send(PROCESSING_FAILED_MESSAGE)
                except Exception as delivery_error:
                    LOGGER.warning(
                        "Last-resort Discord failure delivery also failed: %s",
                        delivery_error,
                    )
                return
            try:
                await self._flush_pending(inbound.conversation_key)
            except Exception as delivery_error:
                # The durable delivery loop will retry this recorded failure.
                LOGGER.warning(
                    "Durable processing-failure delivery will retry: %s",
                    delivery_error,
                )
            return
        try:
            await self._flush_pending(inbound.conversation_key)
        except Exception as error:
            # The output is still pending in SQLite and the delivery loop will
            # retry it. Do not add an untracked fallback message here.
            LOGGER.warning("Initial durable Discord delivery failed: %s", error)

    async def _delivery_loop(self) -> None:
        """Wake for durable outputs produced after an inbound handler returned."""
        orchestrator = self._orchestrator
        if orchestrator is None:
            return
        while not self.is_closed():
            try:
                self._prune_storage_if_due()
                await self._bounded_repository_refresh()
                available = await orchestrator.wait_for_output(timeout=30)
                if not available:
                    continue
                if not self.is_ready():
                    await asyncio.sleep(DISCORD_DELIVERY_BACKOFF_SECONDS)
                    continue
                await self._flush_pending()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                LOGGER.warning("Durable Discord delivery failed and will retry: %s", error)
                await asyncio.sleep(2)

    async def _show_receipt(self, message: discord.Message) -> None:
        """A receipt reaction can appear before inventory or conversation locks."""
        react = getattr(message, "add_reaction", None)
        if not callable(react):
            return
        try:
            await asyncio.wait_for(react("👀"), timeout=VISUAL_FEEDBACK_TIMEOUT_SECONDS)
        except Exception:
            LOGGER.debug("Receipt reaction unavailable; normal processing continues.")

    @asynccontextmanager
    async def _show_typing(self, channel):
        context = None
        entered = False
        try:
            context = channel.typing()
            await asyncio.wait_for(context.__aenter__(), timeout=VISUAL_FEEDBACK_TIMEOUT_SECONDS)
            entered = True
        except Exception:
            LOGGER.debug("Typing indicator unavailable; normal processing continues.")
        try:
            yield
        finally:
            if entered and context is not None:
                try:
                    await asyncio.wait_for(context.__aexit__(None, None, None), timeout=VISUAL_FEEDBACK_TIMEOUT_SECONDS)
                except Exception:
                    LOGGER.debug("Could not close the typing indicator.")

    async def _bounded_repository_refresh(self, *, force: bool = False) -> None:
        try:
            await asyncio.wait_for(self._refresh_worker_repositories(force=force), timeout=REPOSITORY_REFRESH_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            LOGGER.debug("Repository refresh deferred so feedback can be delivered.")

    def _prune_storage_if_due(self) -> bool:
        """Apply bounded retention hourly while a long-lived bot stays online."""
        storage = self._storage
        if storage is None:
            return False
        loop = asyncio.get_running_loop()
        if loop.time() < self._next_storage_prune:
            return False
        self._next_storage_prune = loop.time() + 3_600
        storage.prune(
            memory_retention_seconds=self.settings.memory_retention_days * 86_400,
            operational_retention_seconds=(
                self.settings.operational_retention_days * 86_400
            ),
        )
        return True

    async def _refresh_worker_repositories(self, *, force: bool = False) -> bool:
        """Refresh late/stale host inventory without requiring a bot restart."""
        worker = self._worker
        orchestrator = self._orchestrator
        if worker is None or orchestrator is None:
            return False
        loop = asyncio.get_running_loop()
        if not force and loop.time() < self._next_repository_refresh:
            return False
        async with self._repository_refresh_lock:
            if not force and loop.time() < self._next_repository_refresh:
                return False
            self._next_repository_refresh = loop.time() + 60
            try:
                repositories = await worker.list_repositories()
            except WorkerError as error:
                LOGGER.debug("Could not refresh host repository inventory: %s", error)
                return False
            orchestrator.set_repositories(repositories)
            return True

    async def _flush_pending(self, key: ConversationKey | None = None) -> int:
        orchestrator = self._orchestrator
        if orchestrator is None:
            return 0
        async with self._delivery_lock:
            return await orchestrator.flush_outputs(self._send_outbox_part, key=key)

    async def _send_outbox_part(self, part: OutboxPart) -> discord.Message:
        channel_id = int(part.channel_id)
        channel = self.get_channel(channel_id)
        if channel is None:
            channel = await self.fetch_channel(channel_id)
        sender = getattr(channel, "send", None)
        if sender is None:
            raise RuntimeError(f"Discord channel {channel_id} cannot receive messages.")
        if part.attachment is not None:
            if part.part_index != 0:
                raise ValueError("Only the first Discord output part may carry a CSV attachment.")
            with BytesIO(part.attachment.data) as buffer:
                upload = discord.File(buffer, filename=part.attachment.filename)
                try:
                    return await sender(
                        part.content, file=upload, allowed_mentions=discord.AllowedMentions.none(),
                    )
                finally:
                    upload.close()
        return await sender(part.content)
