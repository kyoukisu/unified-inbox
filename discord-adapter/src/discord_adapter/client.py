from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import mimetypes
from collections.abc import Sequence
from typing import Literal
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import aiohttp
import discord

from discord_adapter.store import AdapterStore

_LOGGER = logging.getLogger(__name__)


class CoreDeliveryError(RuntimeError):
    """Raised when an inbound Discord event cannot reach the core."""


DiscordPresence = Literal["online", "idle", "busy", "offline"]


def normalize_discord_presence(value: object) -> DiscordPresence | None:
    raw_value = getattr(value, "value", value)
    if not isinstance(raw_value, str):
        return None
    normalized = raw_value.lower()
    if normalized in ("online", "idle", "offline"):
        return normalized
    if normalized in ("dnd", "do_not_disturb"):
        return "busy"
    if normalized == "invisible":
        return "offline"
    return None


def is_tenor_view_url(text: str | None) -> bool:
    if text is None or text != text.strip() or any(character.isspace() for character in text):
        return False
    try:
        parsed = urlsplit(text)
    except ValueError:
        return False
    hostname = (parsed.hostname or "").lower()
    return (
        parsed.scheme == "https"
        and hostname in ("tenor.com", "www.tenor.com")
        and parsed.path.startswith("/view/")
    )


async def wait_for_discord_embed(
    message: discord.Message,
    delays: Sequence[float] = (0.5, 1.0, 1.5),
) -> discord.Message:
    if message.embeds or not is_tenor_view_url(message.content):
        return message
    for delay in delays:
        await asyncio.sleep(delay)
        try:
            refreshed = await message.channel.fetch_message(message.id)
        except discord.HTTPException:
            _LOGGER.warning(
                "Unable to refresh Discord Tenor embed for message %s",
                message.id,
                exc_info=True,
            )
            return message
        if refreshed.embeds:
            return refreshed
    return message


def discord_nonce_for_idempotency_key(idempotency_key: str) -> int:
    unsigned = int.from_bytes(
        hashlib.sha256(idempotency_key.encode()).digest()[:8],
        byteorder="big",
        signed=False,
    )
    return unsigned & ((1 << 63) - 1)


def discord_nonce_value(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def discord_embed_attachments(
    embeds: Sequence[discord.Embed],
    excluded_urls: set[str],
) -> tuple[list[dict[str, object]], set[str]]:
    attachments: list[dict[str, object]] = []
    image_urls: set[str] = set()
    seen_urls = set(excluded_urls)
    mime_by_format = {
        "gif": "image/gif",
        "jpeg": "image/jpeg",
        "jpg": "image/jpeg",
        "png": "image/png",
        "webp": "image/webp",
    }

    for embed in embeds:
        if embed.type not in ("image", "gifv"):
            continue
        if embed.type == "gifv" and isinstance(embed.video.proxy_url, str):
            media = embed.video
        else:
            media = embed.image if isinstance(embed.image.proxy_url, str) else embed.thumbnail
        source_url = media.url
        proxy_url = media.proxy_url
        embed_url = embed.url
        if not isinstance(proxy_url, str):
            continue
        attachment_url = proxy_url
        if embed.type == "gifv" and isinstance(source_url, str):
            source_host = (urlsplit(source_url).hostname or "").lower()
            if source_host == "tenor.com" or source_host.endswith(".tenor.com"):
                attachment_url = source_url
        if attachment_url in seen_urls:
            continue
        if isinstance(source_url, str) and source_url in seen_urls:
            continue

        parsed_proxy = urlsplit(attachment_url)
        image_format = parse_qs(parsed_proxy.query).get("format", [""])[0].lower()
        filename_url = source_url if isinstance(source_url, str) else proxy_url
        filename = urlsplit(filename_url).path.rsplit("/", 1)[-1] or "embedded-image"
        media_content_type: object = getattr(media, "content_type", None)
        mime_type = mime_by_format.get(image_format)
        if mime_type is None and isinstance(media_content_type, str):
            mime_type = media_content_type
        mime_type = mime_type or mimetypes.guess_type(filename)[0]
        if mime_type is None or (not mime_type.startswith("image/") and mime_type != "video/mp4"):
            mime_type = "image/jpeg"
            filename = f"{filename}.jpg"
        elif image_format in mime_by_format:
            extension = "jpg" if image_format == "jpeg" else image_format
            stem = filename.rsplit(".", 1)[0]
            filename = f"{stem}.{extension}"

        attachments.append(
            {
                "url": attachment_url,
                "filename": filename,
                "mime_type": mime_type,
            }
        )
        for value in (source_url, proxy_url, embed_url):
            if isinstance(value, str):
                image_urls.add(value)
                image_urls.add(_url_without_query(value))
        seen_urls.add(attachment_url)
        if isinstance(source_url, str):
            seen_urls.add(source_url)

    return attachments, image_urls


def _url_without_query(value: str) -> str:
    try:
        return urlsplit(value)._replace(query="", fragment="").geturl()
    except ValueError:
        return value


def discord_direct_image_attachment(
    text: str | None,
) -> tuple[dict[str, object] | None, set[str]]:
    if text is None:
        return None, set()
    stripped = text.strip()
    try:
        parsed = urlsplit(stripped)
        port = parsed.port
    except ValueError:
        return None, set()
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or port
    ):
        return None, set()

    mime_by_format = {
        "gif": "image/gif",
        "jpeg": "image/jpeg",
        "jpg": "image/jpeg",
        "png": "image/png",
        "webp": "image/webp",
    }
    image_format = parse_qs(parsed.query).get("format", [""])[0].lower()
    filename = parsed.path.rsplit("/", 1)[-1]
    mime_type = mime_by_format.get(image_format) or mimetypes.guess_type(filename)[0]
    if mime_type is None or not mime_type.startswith("image/"):
        return None, set()
    if image_format in mime_by_format:
        extension = "jpg" if image_format == "jpeg" else image_format
        stem = filename.rsplit(".", 1)[0] or "embedded-image"
        filename = f"{stem}.{extension}"

    return (
        {"url": stripped, "filename": filename, "mime_type": mime_type},
        {stripped, _url_without_query(stripped)},
    )


def discord_text_without_embedded_image(text: str | None, image_urls: set[str]) -> str | None:
    if text is None:
        return None
    stripped = text.strip()
    if stripped in image_urls or _url_without_query(stripped) in image_urls:
        return None
    return text


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
        self._event_locks: dict[int, asyncio.Lock] = {}
        self._outbound_locks: dict[str, asyncio.Lock] = {}
        self._spool_wake = asyncio.Event()
        self._spool_task: asyncio.Task[None] | None = None
        self._dm_channels_by_user: dict[int, int] = {}
        self._last_presence: dict[int, tuple[DiscordPresence, str]] = {}
        self._presence_session_id = uuid4().hex
        self._presence_sequence = 0

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
        for channel in self.private_channels:
            self._remember_dm_channel(channel)
        for relationship in self.relationships:
            await self._enqueue_presence(
                relationship.id,
                relationship.user.display_name,
                relationship.status,
            )
        for user_id in self._dm_channels_by_user:
            if user_id in self._last_presence:
                continue
            for guild in self.guilds:
                member = guild.get_member(user_id)
                if member is not None:
                    await self._enqueue_presence(member.id, member.display_name, member.status)
                    break

    async def on_presence_update(
        self,
        before: discord.Relationship | discord.Member,
        after: discord.Relationship | discord.Member,
    ) -> None:
        del before
        display_name = (
            after.user.display_name
            if isinstance(after, discord.Relationship)
            else after.display_name
        )
        await self._enqueue_presence(after.id, display_name, after.status)

    async def on_message(self, message: discord.Message) -> None:
        if self.user is None or message.guild is not None:
            return
        self._remember_dm_channel(message.channel)
        event_lock = self._event_locks.setdefault(message.channel.id, asyncio.Lock())
        async with event_lock:
            message = await wait_for_discord_embed(message)
            direction = "outbound_native" if message.author.id == self.user.id else "inbound"
            await self._enqueue_message(message, direction)

    def _remember_dm_channel(self, channel: object) -> None:
        if isinstance(channel, discord.DMChannel):
            self._dm_channels_by_user[channel.recipient.id] = channel.id

    async def _enqueue_presence(
        self,
        user_id: int,
        display_name: str,
        raw_status: object,
    ) -> None:
        channel_id = self._dm_channels_by_user.get(user_id)
        status = normalize_discord_presence(raw_status)
        if channel_id is None or status is None:
            return
        channel = self.get_channel(channel_id)
        if isinstance(channel, discord.DMChannel):
            display_name = channel.recipient.display_name
        current = (status, display_name)
        if self._last_presence.get(user_id) == current:
            return
        self._presence_sequence += 1
        payload: dict[str, object] = {
            "kind": "presence",
            "platform": "discord",
            "event_id": (
                f"presence:{channel_id}:{self._presence_session_id}:{self._presence_sequence}"
            ),
            "conversation_id": str(channel_id),
            "display_name": display_name,
            "status": status,
        }
        try:
            created = self._store.enqueue_event(payload)
        except Exception:
            _LOGGER.critical(
                "Unable to persist Discord presence for user %s; stopping adapter",
                user_id,
                exc_info=True,
            )
            await self.close()
            raise
        self._last_presence[user_id] = current
        if created:
            self._spool_wake.set()

    async def _enqueue_message(self, message: discord.Message, direction: str) -> None:
        if direction == "outbound_native":
            nonce_value = discord_nonce_value(message.nonce)
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
        attachment_urls: set[str] = set()
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
            attachment_urls.add(attachment.url)

        embedded_attachments, embedded_image_urls = discord_embed_attachments(
            message.embeds,
            attachment_urls,
        )
        attachments.extend(embedded_attachments)
        if not embedded_attachments:
            direct_attachment, direct_image_urls = discord_direct_image_attachment(
                message.content if message.content else None
            )
            if direct_attachment is not None:
                embedded_image_urls.update(direct_image_urls)
                if direct_attachment["url"] not in attachment_urls:
                    attachments.append(direct_attachment)

        text = discord_text_without_embedded_image(
            message.content if message.content else None,
            embedded_image_urls,
        )
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
        lock = self._outbound_locks.setdefault(conversation_id, asyncio.Lock())
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
