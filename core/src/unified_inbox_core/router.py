from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import cast

import aiohttp

from unified_inbox_core.adapter import AdapterClient
from unified_inbox_core.animation import DISCORD_GIF_LIMIT_BYTES, convert_mp4_to_gif
from unified_inbox_core.db import Database
from unified_inbox_core.errors import PermanentDeliveryError
from unified_inbox_core.media import MediaDownloadError, download_media
from unified_inbox_core.models import (
    Conversation,
    DeliveryJob,
    EnqueueResult,
    ExternalEvent,
    InboundEvent,
    OutboundMessage,
    PresenceEvent,
    PresenceStatus,
    external_event_from_mapping,
)
from unified_inbox_core.telegram import TelegramClient, TelegramImage, split_utf16, utf16_length

_LOGGER = logging.getLogger(__name__)
_PLATFORM_ICON = {"discord": "👾 Discord", "steam": "🎮 Steam"}
_PRESENCE_ICON = {"online": "🟢", "idle": "🟡", "busy": "🔴", "offline": "⚫"}


class Router:
    def __init__(
        self,
        db: Database,
        telegram: TelegramClient,
        adapters: AdapterClient,
        session: aiohttp.ClientSession,
        telegram_chat_id: int,
        telegram_allowed_user_id: int,
        max_image_bytes: int,
        outbox_telegram: TelegramClient | None = None,
    ) -> None:
        self._db = db
        self._telegram = telegram
        self._outbox_telegram = outbox_telegram or telegram
        self._adapters = adapters
        self._session = session
        self._chat_id = telegram_chat_id
        self._allowed_user_id = telegram_allowed_user_id
        self._max_image_bytes = max_image_bytes
        self._conversation_lock = asyncio.Lock()

    def enqueue_inbound(self, event: ExternalEvent) -> EnqueueResult:
        return self._db.enqueue_external_event(
            event.platform,
            event.event_id,
            self._conversation_key_values(event.platform, event.conversation_id),
            json.dumps(event.to_mapping(), ensure_ascii=False, separators=(",", ":")),
        )

    def enqueue_telegram_update(self, update: dict[str, object]) -> EnqueueResult:
        update_id = update.get("update_id")
        if not isinstance(update_id, int):
            raise ValueError("Telegram update has no integer update_id")
        raw_message = update.get("message") or update.get("edited_message")
        topic_id: int | None = None
        telegram_message_id: int | None = None
        text: str | None = None
        if isinstance(raw_message, dict):
            message = cast(dict[str, object], raw_message)
            raw_topic_id = message.get("message_thread_id")
            topic_id = raw_topic_id if isinstance(raw_topic_id, int) else None
            raw_message_id = message.get("message_id")
            telegram_message_id = raw_message_id if isinstance(raw_message_id, int) else None
            raw_text = message.get("text")
            text = raw_text if isinstance(raw_text, str) else None

        conversation = (
            self._db.get_conversation_by_topic(topic_id) if topic_id is not None else None
        )
        if text is not None and text.startswith("/"):
            conversation_key = f"control:{topic_id or 0}"
        elif conversation is not None:
            conversation_key = self._conversation_key(conversation)
        else:
            conversation_key = f"telegram-topic:{topic_id or 0}"

        return self._db.enqueue_telegram_update(
            str(update_id),
            conversation_key,
            json.dumps(update, ensure_ascii=False, separators=(",", ":")),
            telegram_message_id,
            update_id + 1,
        )

    async def process_job(self, job: DeliveryJob) -> None:
        try:
            raw: object = json.loads(job.payload_json)
        except json.JSONDecodeError as exc:
            raise PermanentDeliveryError("stored delivery payload is invalid JSON") from exc
        if not isinstance(raw, dict):
            raise PermanentDeliveryError("stored delivery payload is not an object")
        payload = cast(dict[str, object], raw)
        if job.kind == "route_external_event":
            try:
                event = external_event_from_mapping(payload)
            except ValueError as exc:
                raise PermanentDeliveryError(str(exc)) from exc
            if isinstance(event, PresenceEvent):
                await self._process_presence(job, event)
            else:
                await self._process_inbound(job, event)
            return
        if job.kind == "route_telegram_update":
            await self._process_telegram_update(job, payload)
            return
        raise PermanentDeliveryError(f"unsupported delivery job kind: {job.kind}")

    async def set_pending_reaction(self, update: dict[str, object]) -> None:
        message = update.get("message")
        if not isinstance(message, dict):
            return
        typed_message = cast(dict[str, object], message)
        if not self._is_authorized_message(typed_message) or self._is_command(typed_message):
            return
        message_id = typed_message.get("message_id")
        if isinstance(message_id, int):
            await self._set_reaction_best_effort(message_id, "👀")

    async def set_delivery_reaction(self, job: DeliveryJob, emoji: str) -> None:
        if job.source != "telegram" or job.telegram_message_id is None:
            return
        try:
            raw: object = json.loads(job.payload_json)
        except json.JSONDecodeError:
            return
        if not isinstance(raw, dict):
            return
        update = cast(dict[str, object], raw)
        message = update.get("message") or update.get("edited_message")
        if not isinstance(message, dict):
            return
        typed_message = cast(dict[str, object], message)
        if not self._is_authorized_message(typed_message) or self._is_command(typed_message):
            return
        await self._set_reaction_best_effort(job.telegram_message_id, emoji)

    async def _process_inbound(self, job: DeliveryJob, event: InboundEvent) -> None:
        conversation = await self._resolve_conversation(event)
        relay = self._outbox_telegram if event.direction == "outbound_native" else self._telegram

        reply_id = None
        if event.reply_to_message_id is not None:
            reply_id = self._db.telegram_message_for_external(
                conversation.id,
                event.reply_to_message_id,
            )

        sent_ids: list[int] = []
        caption = (
            event.text
            if event.attachments and event.text is not None and utf16_length(event.text) <= 1024
            else None
        )
        remaining_text = None if caption is not None else event.text

        for index, attachment in enumerate(event.attachments):
            part_key = f"attachment:{index}"
            existing = self._db.get_delivery_part(job.id, part_key)
            if existing is not None:
                sent_ids.append(int(existing))
                continue

            self._db.renew_job_lease(job.id, 300)
            try:
                content = await download_media(
                    self._session,
                    event.platform,
                    attachment.url,
                    self._max_image_bytes,
                )
                send_media = (
                    relay.send_animation
                    if attachment.mime_type in ("image/gif", "video/mp4")
                    else relay.send_photo
                )
                message_id = await send_media(
                    self._chat_id,
                    conversation.telegram_topic_id,
                    content,
                    attachment.filename,
                    attachment.mime_type,
                    caption=caption if index == 0 else None,
                    reply_to_message_id=reply_id if index == 0 else None,
                )
                self._db.store_delivery_part(job.id, part_key, str(message_id))
                sent_ids.append(message_id)
            except MediaDownloadError as exc:
                _LOGGER.warning(
                    "Unable to relay %s media for event %s: %s",
                    event.platform,
                    event.event_id,
                    exc,
                )
                fallback = attachment.url
                if index == 0 and caption:
                    fallback = f"{caption}\n{attachment.url}"
                fallback_ids = await self._send_complete_text(
                    job,
                    relay,
                    conversation.telegram_topic_id,
                    fallback,
                    reply_id if index == 0 else None,
                    f"attachment:{index}",
                )
                sent_ids.extend(fallback_ids)

        if remaining_text:
            sent_ids.extend(
                await self._send_complete_text(
                    job,
                    relay,
                    conversation.telegram_topic_id,
                    remaining_text,
                    reply_id if not sent_ids else None,
                    "text",
                )
            )

        if not sent_ids:
            raise PermanentDeliveryError("inbound event produced no Telegram messages")

        self._db.store_message_copy(
            conversation.id,
            event.message_id,
            sent_ids[0],
            "outbound" if event.direction == "outbound_native" else "inbound",
        )

    async def _process_presence(self, job: DeliveryJob, event: PresenceEvent) -> None:
        current_status = self._db.get_presence(event.platform, event.conversation_id)
        conversation = self._db.get_conversation(event.platform, event.conversation_id)
        if conversation is None:
            self._db.store_presence(event.platform, event.conversation_id, event.status)
            return

        display_name_changed = conversation.display_name != event.display_name
        if current_status == event.status and not display_name_changed:
            return

        part_key = "topic"
        if self._db.get_delivery_part(job.id, part_key) is None:
            await self._telegram.edit_topic(
                self._chat_id,
                conversation.telegram_topic_id,
                self._topic_name(event.platform, event.display_name, event.status),
            )
            self._db.store_delivery_part(
                job.id,
                part_key,
                str(conversation.telegram_topic_id),
            )
        self._db.store_presence(event.platform, event.conversation_id, event.status)
        if display_name_changed:
            self._db.update_display_name(conversation.id, event.display_name)

    async def _process_telegram_update(
        self,
        job: DeliveryJob,
        update: dict[str, object],
    ) -> None:
        raw_message = update.get("message") or update.get("edited_message")
        if not isinstance(raw_message, dict):
            return
        message = cast(dict[str, object], raw_message)
        chat = message.get("chat")
        sender = message.get("from")
        typed_chat = cast(dict[str, object], chat) if isinstance(chat, dict) else {}
        typed_sender = cast(dict[str, object], sender) if isinstance(sender, dict) else {}
        message_id = message.get("message_id")
        if (
            typed_chat.get("id") == self._chat_id
            and typed_sender.get("is_bot") is True
            and "forum_topic_edited" in message
            and isinstance(message_id, int)
        ):
            part_key = "delete-topic-edit"
            if self._db.get_delivery_part(job.id, part_key) is None:
                await self._telegram.delete_message(self._chat_id, message_id)
                self._db.store_delivery_part(job.id, part_key, str(message_id))
            return
        if not self._is_authorized_message(message):
            _LOGGER.warning("Rejected unauthorized Telegram update %s", job.event_id)
            return

        topic_id = message.get("message_thread_id")
        if not isinstance(topic_id, int):
            return
        conversation = self._db.get_conversation_by_topic(topic_id)
        if conversation is None:
            await self._handle_unmapped_topic(job, message, topic_id)
            return

        text_value = message.get("text") or message.get("caption")
        text = text_value if isinstance(text_value, str) and text_value else None
        if text and text.startswith("/"):
            await self._handle_command(job, conversation, topic_id, text, message)
            return

        image = await self._telegram.download_message_image(message)
        if (
            image is not None
            and conversation.platform != "discord"
            and not image.mime_type.startswith("image/")
        ):
            raise PermanentDeliveryError("Telegram animations can only be relayed to Discord")
        if (
            image is not None
            and conversation.platform == "discord"
            and image.mime_type.lower() == "video/mp4"
        ):
            self._db.renew_job_lease(job.id, 300)
            converted = await convert_mp4_to_gif(
                image.content,
                min(self._max_image_bytes, DISCORD_GIF_LIMIT_BYTES),
            )
            if converted is not None:
                image = TelegramImage(
                    content=converted,
                    filename=f"{Path(image.filename).stem}.gif",
                    mime_type="image/gif",
                )
        if text is None and image is None:
            if any(key in message for key in ("document", "video", "audio", "voice")):
                raise PermanentDeliveryError("this Telegram media type is not supported yet")
            return

        reply_to_external = self._resolve_outbound_reply(conversation, message)
        text_chunks = (
            split_utf16(text, 2000 if conversation.platform == "discord" else 4096) if text else []
        )
        parts: list[tuple[str | None, bool]] = []
        if image is not None:
            first_text = text_chunks.pop(0) if text_chunks else None
            parts.append((first_text, True))
        parts.extend((chunk, False) for chunk in text_chunks)
        if not parts and text is not None:
            parts.append((text, False))

        delivered_ids: list[str] = []
        update_id = update.get("update_id")
        if not isinstance(update_id, int):
            raise PermanentDeliveryError("Telegram update has no integer update_id")

        for index, (part_text, include_image) in enumerate(parts):
            part_key = f"external:{index}"
            existing = self._db.get_delivery_part(job.id, part_key)
            if existing is not None:
                delivered_ids.append(existing)
                continue
            self._db.renew_job_lease(job.id, 300)
            delivery = await self._adapters.send(
                conversation.platform,
                OutboundMessage(
                    idempotency_key=(
                        f"telegram:{update_id}"
                        if index == 0
                        else f"telegram:{update_id}:part:{index}"
                    ),
                    conversation_id=conversation.external_chat_id,
                    text=part_text,
                    reply_to_message_id=reply_to_external if index == 0 else None,
                    image=image.content if include_image and image is not None else None,
                    image_filename=image.filename if include_image and image is not None else None,
                    image_mime_type=image.mime_type
                    if include_image and image is not None
                    else None,
                ),
            )
            self._db.store_delivery_part(job.id, part_key, delivery.message_id)
            delivered_ids.append(delivery.message_id)

        if not delivered_ids:
            raise PermanentDeliveryError("Telegram update produced no external messages")
        telegram_message_id = message.get("message_id")
        if isinstance(telegram_message_id, int):
            self._db.store_message_copy(
                conversation.id,
                delivered_ids[0],
                telegram_message_id,
                "outbound",
            )

    async def _send_complete_text(
        self,
        job: DeliveryJob,
        relay: TelegramClient,
        topic_id: int,
        text: str,
        reply_to_message_id: int | None,
        part_prefix: str,
    ) -> list[int]:
        sent_ids: list[int] = []
        for index, chunk in enumerate(split_utf16(text, 4096)):
            part_key = part_prefix if index == 0 else f"{part_prefix}:{index}"
            existing = self._db.get_delivery_part(job.id, part_key)
            if existing is not None:
                sent_ids.append(int(existing))
                continue
            self._db.renew_job_lease(job.id, 300)
            message_id = await relay.send_text(
                self._chat_id,
                topic_id,
                chunk,
                reply_to_message_id=reply_to_message_id if index == 0 else None,
            )
            self._db.store_delivery_part(job.id, part_key, str(message_id))
            sent_ids.append(message_id)
        return sent_ids

    async def _resolve_conversation(self, event: InboundEvent) -> Conversation:
        conversation = self._db.get_conversation(event.platform, event.conversation_id)
        if conversation is not None:
            if conversation.display_name != event.display_name:
                topic_name = self._topic_name(
                    event.platform,
                    event.display_name,
                    self._db.get_presence(event.platform, event.conversation_id),
                )
                await self._telegram.edit_topic(
                    self._chat_id,
                    conversation.telegram_topic_id,
                    topic_name,
                )
                self._db.update_display_name(conversation.id, event.display_name)
                return Conversation(
                    id=conversation.id,
                    platform=conversation.platform,
                    external_chat_id=conversation.external_chat_id,
                    display_name=event.display_name,
                    telegram_topic_id=conversation.telegram_topic_id,
                )
            return conversation

        async with self._conversation_lock:
            conversation = self._db.get_conversation(event.platform, event.conversation_id)
            if conversation is not None:
                return conversation
            topic_id = await self._telegram.create_topic(
                self._chat_id,
                self._topic_name(
                    event.platform,
                    event.display_name,
                    self._db.get_presence(event.platform, event.conversation_id),
                ),
            )
            return self._db.create_conversation(
                event.platform,
                event.conversation_id,
                event.display_name,
                topic_id,
            )

    def _is_authorized_message(self, message: dict[str, object]) -> bool:
        chat = message.get("chat")
        sender = message.get("from")
        if not isinstance(chat, dict) or not isinstance(sender, dict):
            return False
        typed_chat = cast(dict[str, object], chat)
        typed_sender = cast(dict[str, object], sender)
        return (
            typed_chat.get("id") == self._chat_id
            and typed_sender.get("id") == self._allowed_user_id
        )

    def _resolve_outbound_reply(
        self,
        conversation: Conversation,
        message: dict[str, object],
    ) -> str | None:
        reply = message.get("reply_to_message")
        if not isinstance(reply, dict):
            return None
        typed_reply = cast(dict[str, object], reply)
        telegram_message_id = typed_reply.get("message_id")
        if not isinstance(telegram_message_id, int):
            return None
        return self._db.external_message_for_telegram(conversation.id, telegram_message_id)

    async def _handle_command(
        self,
        job: DeliveryJob,
        conversation: Conversation,
        topic_id: int,
        text: str,
        message: dict[str, object],
    ) -> None:
        command, _, argument = text.partition(" ")
        command = command.split("@", 1)[0].lower()
        conversation_key = self._conversation_key(conversation)
        response: str | None = None
        if command == "/status":
            status = await self._adapters.status(conversation.platform)
            response = f"{conversation.platform}: {status}\nqueue: {self._db.job_counts()}"
        elif command == "/failures":
            failures = self._db.list_failures(conversation_key)
            legacy_failures = self._db.list_legacy_failures()
            lines = [
                f"#{failure.job_id} · attempts={failure.attempt_count} · {failure.error}"
                for failure in failures
            ]
            lines.extend(
                f"legacy {failure.source}:{failure.event_id} · {failure.error}"
                for failure in legacy_failures
            )
            response = "\n".join(lines) if lines else "Нет недоставленных сообщений в этом диалоге."
        elif command == "/retry":
            value = argument.strip().lower()
            retry_all = value == "all"
            retry_id: int | None = None
            if value and not retry_all:
                try:
                    retry_id = int(value.lstrip("#"))
                except ValueError:
                    response = "Использование: /retry, /retry <job-id> или /retry all"
            if response is None:
                retried = self._db.retry_failed_jobs(
                    conversation_key,
                    job_id=retry_id,
                    retry_all=retry_all,
                )
                response = (
                    f"Поставлено в очередь: {', '.join(f'#{item}' for item in retried)}"
                    if retried
                    else "Подходящих failed jobs нет."
                )
        elif command == "/rename" and argument.strip():
            display_name = argument.strip()
            await self._telegram.edit_topic(
                self._chat_id,
                topic_id,
                self._topic_name(conversation.platform, display_name),
            )
            self._db.update_display_name(conversation.id, display_name)
        elif command in ("/close", "/archive"):
            await self._telegram.close_topic(self._chat_id, topic_id)
        else:
            response = "Команды: /status, /failures, /retry [job-id|all], /rename <имя>, /close"

        if response:
            await self._send_complete_text(
                job,
                self._telegram,
                topic_id,
                response,
                None,
                "command-response",
            )

    async def _handle_unmapped_topic(
        self,
        job: DeliveryJob,
        message: dict[str, object],
        topic_id: int,
    ) -> None:
        text = message.get("text")
        if text == "/status":
            discord, steam = await asyncio.gather(
                self._adapters.status("discord"),
                self._adapters.status("steam"),
            )
            await self._send_complete_text(
                job,
                self._telegram,
                topic_id,
                f"discord: {discord}\nsteam: {steam}\nqueue: {self._db.job_counts()}",
                None,
                "command-response",
            )

    async def _set_reaction_best_effort(self, message_id: int, emoji: str) -> None:
        try:
            await self._telegram.set_reaction(self._chat_id, message_id, emoji)
        except Exception as exc:
            _LOGGER.warning(
                "Unable to set Telegram delivery reaction on message %s: %s",
                message_id,
                exc,
            )

    @staticmethod
    def _is_command(message: dict[str, object]) -> bool:
        text = message.get("text")
        return isinstance(text, str) and text.startswith("/")

    @staticmethod
    def _conversation_key(conversation: Conversation) -> str:
        return Router._conversation_key_values(
            conversation.platform,
            conversation.external_chat_id,
        )

    @staticmethod
    def _conversation_key_values(platform: str, external_chat_id: str) -> str:
        return f"{platform}:{external_chat_id}"

    @staticmethod
    def _topic_name(
        platform: str,
        display_name: str,
        status: PresenceStatus | None = None,
    ) -> str:
        presence = f"{_PRESENCE_ICON[status]} " if status is not None else ""
        name = f"{presence}{_PLATFORM_ICON[platform]} · {display_name}"
        if utf16_length(name) <= 128:
            return name
        return f"{split_utf16(name, 127)[0]}…"
