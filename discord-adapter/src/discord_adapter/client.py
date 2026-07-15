from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import mimetypes

import aiohttp
import discord

_LOGGER = logging.getLogger(__name__)


class CoreDeliveryError(RuntimeError):
    """Raised when an inbound Discord event cannot reach the core."""


class DiscordBridgeClient(discord.Client):
    def __init__(
        self,
        session: aiohttp.ClientSession,
        core_url: str,
        internal_token: str,
    ) -> None:
        super().__init__()
        self._session = session
        self._core_url = core_url
        self._internal_token = internal_token
        self._delivery_tasks: set[asyncio.Task[None]] = set()

    async def on_ready(self) -> None:
        if self.user is None:
            raise RuntimeError("Discord ready event has no current user")
        _LOGGER.info("Discord user session connected as %s (%s)", self.user, self.user.id)

    async def on_message(self, message: discord.Message) -> None:
        if self.user is None or message.author.id == self.user.id or message.guild is not None:
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

        text = message.content.strip() or None
        if text is None and not attachments:
            return

        reply_to = None
        if message.reference is not None and message.reference.message_id is not None:
            reply_to = str(message.reference.message_id)

        display_name = self._conversation_name(message)
        payload: dict[str, object] = {
            "platform": "discord",
            "event_id": str(message.id),
            "conversation_id": str(message.channel.id),
            "display_name": display_name,
            "sender_id": str(message.author.id),
            "sender_name": message.author.display_name,
            "message_id": str(message.id),
            "text": text,
            "reply_to_message_id": reply_to,
            "attachments": attachments,
        }
        task = asyncio.create_task(
            self._deliver_to_core(payload),
            name=f"discord-inbound-{message.id}",
        )
        self._delivery_tasks.add(task)
        task.add_done_callback(self._delivery_tasks.discard)

    async def close(self) -> None:
        for task in self._delivery_tasks:
            task.cancel()
        if self._delivery_tasks:
            await asyncio.gather(*self._delivery_tasks, return_exceptions=True)
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
        channel_id = int(conversation_id)
        channel = self.get_channel(channel_id)
        if channel is None:
            channel = await self.fetch_channel(channel_id)
        if not isinstance(channel, (discord.DMChannel, discord.GroupChannel)):
            raise ValueError("conversation is not a Discord direct-message channel")

        reference: discord.MessageReference | None = None
        if reply_to_message_id is not None:
            reference = discord.MessageReference(
                message_id=int(reply_to_message_id),
                channel_id=channel.id,
            )

        file: discord.File | None = None
        if image is not None:
            file = discord.File(
                io.BytesIO(image),
                filename=image_filename or "image",
            )

        nonce = int.from_bytes(
            hashlib.sha256(idempotency_key.encode()).digest()[:8],
            byteorder="big",
            signed=False,
        )
        if file is not None and reference is not None:
            sent = await channel.send(
                content=text,
                file=file,
                reference=reference,
                mention_author=False,
                nonce=nonce,
            )
        elif file is not None:
            sent = await channel.send(content=text, file=file, nonce=nonce)
        elif reference is not None:
            sent = await channel.send(
                content=text,
                reference=reference,
                mention_author=False,
                nonce=nonce,
            )
        else:
            sent = await channel.send(content=text, nonce=nonce)
        return str(sent.id)

    async def _deliver_to_core(self, payload: dict[str, object]) -> None:
        headers = {"Authorization": f"Bearer {self._internal_token}"}
        delay = 1.0
        for attempt in range(5):
            try:
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
            except (aiohttp.ClientError, TimeoutError, CoreDeliveryError):
                if attempt == 4:
                    _LOGGER.exception(
                        "Failed to deliver Discord event %s to core",
                        payload.get("event_id"),
                    )
                    return
                await asyncio.sleep(delay)
                delay = min(delay * 2, 15)

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
