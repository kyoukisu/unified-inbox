from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import mimetypes

import aiohttp
import discord

from discord_adapter.store import AdapterStore

_LOGGER = logging.getLogger(__name__)


class CoreDeliveryError(RuntimeError):
    """Raised when an inbound Discord event cannot reach the core."""


def discord_nonce_for_idempotency_key(idempotency_key: str) -> int:
    unsigned = int.from_bytes(
        hashlib.sha256(idempotency_key.encode()).digest()[:8],
        byteorder="big",
        signed=False,
    )
    return unsigned & ((1 << 63) - 1)


class DiscordBridgeClient(discord.Client):
    def __init__(
        self,
        session: aiohttp.ClientSession,
        core_url: str,
        internal_token: str,
        store: AdapterStore,
    ) -> None:
        super().__init__()
        self._session = session
        self._core_url = core_url
        self._internal_token = internal_token
        self._store = store
        self._bridge_message_ids: set[int] = set()
        self._bridge_nonces: set[int] = set()
        self._event_lock = asyncio.Lock()
        self._outbound_locks: dict[str, asyncio.Lock] = {}
        self._spool_wake = asyncio.Event()
        self._spool_task: asyncio.Task[None] | None = None

    @property
    def spool_task(self) -> asyncio.Task[None] | None:
        return self._spool_task

    @property
    def spool_alive(self) -> bool:
        return self._spool_task is not None and not self._spool_task.done()

    @property
    def pending_count(self) -> int:
        return self._store.pending_count()

    def start_spool(self) -> None:
        if self._spool_task is None:
            self._spool_task = asyncio.create_task(
                self._deliver_pending_loop(),
                name="discord-core-spool",
            )

    async def on_ready(self) -> None:
        if self.user is None:
            raise RuntimeError("Discord ready event has no current user")
        _LOGGER.info("Discord user session connected as %s (%s)", self.user, self.user.id)
        self.start_spool()

    async def on_message(self, message: discord.Message) -> None:
        if self.user is None or message.guild is not None:
            return
        async with self._event_lock:
            direction = "outbound_native" if message.author.id == self.user.id else "inbound"
            await self._enqueue_message(message, direction)

    async def _enqueue_message(self, message: discord.Message, direction: str) -> None:
        if direction == "outbound_native":
            nonce = message.nonce
            nonce_value = nonce if isinstance(nonce, int) else None
            if (
                message.id in self._bridge_message_ids
                or self._store.is_bridge_message(message.id)
                or (nonce_value is not None and nonce_value in self._bridge_nonces)
                or (nonce_value is not None and self._store.is_bridge_nonce(nonce_value))
            ):
                self._bridge_message_ids.discard(message.id)
                if nonce_value is not None:
                    self._bridge_nonces.discard(nonce_value)
                return

        attachments: list[dict[str, object]] = []
        for attachment in message.attachments:
            mime_type = attachment.content_type or mimetypes.guess_type(attachment.filename)[0]
            if mime_type is None or not mime_type.startswith("image/"):
                continue
            attachments.append(
                {
                    "url": attachment.url,
                    "filename": attachment.filename,
                    "mime_type": mime_type,
                }
            )

        text = message.content if message.content else None
        if text is None and not attachments:
            return

        reply_to = None
        if message.reference is not None and message.reference.message_id is not None:
            reply_to = str(message.reference.message_id)

        payload: dict[str, object] = {
            "platform": "discord",
            "event_id": str(message.id),
            "conversation_id": str(message.channel.id),
            "display_name": self._conversation_name(message),
            "sender_id": str(message.author.id),
            "sender_name": message.author.display_name,
            "message_id": str(message.id),
            "text": text,
            "reply_to_message_id": reply_to,
            "attachments": attachments,
            "direction": direction,
        }
        try:
            created = self._store.enqueue_event(payload)
        except Exception:
            _LOGGER.critical(
                "Unable to persist observed Discord event %s; stopping adapter",
                message.id,
                exc_info=True,
            )
            await self.close()
            raise
        if created:
            self._spool_wake.set()

    async def close(self) -> None:
        if self._spool_task is not None:
            self._spool_task.cancel()
            await asyncio.gather(self._spool_task, return_exceptions=True)
            self._spool_task = None
        await super().close()

    async def send_message(
        self,
        conversation_id: str,
        idempotency_key: str,
        text: str | None,
        reply_to_message_id: str | None,
        image: bytes | None,
        image_filename: str | None,
    ) -> str:
        lock = self._outbound_locks.setdefault(idempotency_key, asyncio.Lock())
        async with lock:
            existing = self._store.get_outbound(idempotency_key)
            if existing is not None and existing.state == "completed" and existing.message_id:
                return existing.message_id

            channel_id = int(conversation_id)
            channel = self.get_channel(channel_id)
            if channel is None:
                channel = await self.fetch_channel(channel_id)
            if not isinstance(channel, (discord.DMChannel, discord.GroupChannel)):
                raise ValueError("conversation is not a Discord direct-message channel")

            nonce = discord_nonce_for_idempotency_key(idempotency_key)
            record = self._store.begin_outbound(idempotency_key, conversation_id, nonce)
            self._remember_bridge_value(self._bridge_nonces, nonce)
            if existing is not None or record.message_id is not None:
                reconciled = await self._find_message_by_nonce(channel, nonce)
                if reconciled is not None:
                    message_id = str(reconciled.id)
                    self._store.complete_outbound(idempotency_key, message_id)
                    self._remember_bridge_value(self._bridge_message_ids, reconciled.id)
                    return message_id

            reference: discord.MessageReference | None = None
            if reply_to_message_id is not None:
                reference = discord.MessageReference(
                    message_id=int(reply_to_message_id),
                    channel_id=channel.id,
                )

            try:
                sent = await self._send_discord_message(
                    channel,
                    text,
                    image,
                    image_filename,
                    reference,
                    nonce,
                )
            except (discord.NotFound, discord.HTTPException) as exc:
                if reference is None or not self._missing_reply_target(exc):
                    raise
                sent = await self._send_discord_message(
                    channel,
                    text,
                    image,
                    image_filename,
                    None,
                    nonce,
                )

            message_id = str(sent.id)
            self._store.complete_outbound(idempotency_key, message_id)
            self._remember_bridge_value(self._bridge_message_ids, sent.id)
            return message_id

    async def _send_discord_message(
        self,
        channel: discord.DMChannel | discord.GroupChannel,
        text: str | None,
        image: bytes | None,
        image_filename: str | None,
        reference: discord.MessageReference | None,
        nonce: int,
    ) -> discord.Message:
        file = (
            discord.File(io.BytesIO(image), filename=image_filename or "image")
            if image is not None
            else None
        )
        if file is not None and reference is not None:
            return await channel.send(
                content=text,
                file=file,
                reference=reference,
                mention_author=False,
                nonce=nonce,
            )
        if file is not None:
            return await channel.send(content=text, file=file, nonce=nonce)
        if reference is not None:
            return await channel.send(
                content=text,
                reference=reference,
                mention_author=False,
                nonce=nonce,
            )
        return await channel.send(content=text, nonce=nonce)

    async def _find_message_by_nonce(
        self,
        channel: discord.DMChannel | discord.GroupChannel,
        nonce: int,
    ) -> discord.Message | None:
        if self.user is None:
            return None
        async for message in channel.history(limit=100):
            if message.author.id == self.user.id and str(message.nonce) == str(nonce):
                return message
        return None

    async def _deliver_pending_loop(self) -> None:
        delay = 1.0
        while True:
            pending = self._store.peek_event()
            if pending is None:
                self._spool_wake.clear()
                try:
                    await asyncio.wait_for(self._spool_wake.wait(), timeout=1)
                except TimeoutError:
                    pass
                continue
            try:
                await self._post_to_core(pending.payload)
            except (aiohttp.ClientError, TimeoutError, CoreDeliveryError) as exc:
                self._store.fail_event_attempt(pending.sequence, str(exc))
                _LOGGER.warning(
                    "Discord event %s remains queued after core delivery failure: %s",
                    pending.event_id,
                    exc,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 300)
            else:
                self._store.delete_event(pending.sequence)
                delay = 1.0

    async def _post_to_core(self, payload: dict[str, object]) -> None:
        headers = {"Authorization": f"Bearer {self._internal_token}"}
        async with self._session.post(
            f"{self._core_url}/v1/events",
            json=payload,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=90),
        ) as response:
            if response.status < 300:
                return
            body = await response.text()
            raise CoreDeliveryError(f"core returned HTTP {response.status}: {body[:300]}")

    @staticmethod
    def _missing_reply_target(exc: discord.HTTPException) -> bool:
        return exc.status == 404 or getattr(exc, "code", 0) == 10008

    @staticmethod
    def _remember_bridge_value(values: set[int], value: int) -> None:
        if len(values) >= 2048:
            values.pop()
        values.add(value)

    @staticmethod
    def _conversation_name(message: discord.Message) -> str:
        channel = message.channel
        if isinstance(channel, discord.DMChannel):
            return channel.recipient.display_name
        if isinstance(channel, discord.GroupChannel):
            if channel.name:
                return channel.name
            names = [user.display_name for user in channel.recipients]
            if names:
                return ", ".join(names)[:100]
        return message.author.display_name
