from __future__ import annotations

import asyncio
import logging
from typing import cast

import aiohttp

from unified_inbox_core.adapter import AdapterClient
from unified_inbox_core.db import Database
from unified_inbox_core.media import MediaDownloadError, download_media
from unified_inbox_core.models import Conversation, InboundEvent, OutboundMessage
from unified_inbox_core.telegram import TelegramClient

_LOGGER = logging.getLogger(__name__)
_PLATFORM_ICON = {"discord": "👾 Discord", "steam": "🎮 Steam"}


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
    ) -> None:
        self._db = db
        self._telegram = telegram
        self._adapters = adapters
        self._session = session
        self._chat_id = telegram_chat_id
        self._allowed_user_id = telegram_allowed_user_id
        self._max_image_bytes = max_image_bytes
        self._conversation_lock = asyncio.Lock()

    async def handle_inbound(self, event: InboundEvent) -> bool:
        if not self._db.claim_event(event.platform, event.event_id):
            return False

        try:
            conversation = await self._resolve_conversation(event)
            reply_id = None
            if event.reply_to_message_id is not None:
                reply_id = self._db.telegram_message_for_external(
                    conversation.id,
                    event.reply_to_message_id,
                )

            sent_ids: list[int] = []
            remaining_text = event.text
            for index, attachment in enumerate(event.attachments):
                try:
                    content = await download_media(
                        self._session,
                        event.platform,
                        attachment.url,
                        self._max_image_bytes,
                    )
                    sent_ids.append(
                        await self._telegram.send_photo(
                            self._chat_id,
                            conversation.telegram_topic_id,
                            content,
                            attachment.filename,
                            attachment.mime_type,
                            caption=remaining_text if index == 0 else None,
                            reply_to_message_id=reply_id if index == 0 else None,
                        )
                    )
                    if index == 0:
                        remaining_text = None
                except MediaDownloadError as exc:
                    _LOGGER.warning(
                        "Unable to relay %s media for event %s: %s",
                        event.platform,
                        event.event_id,
                        exc,
                    )
                    fallback = attachment.url
                    if index == 0 and remaining_text:
                        fallback = f"{remaining_text}\n{attachment.url}"
                        remaining_text = None
                    sent_ids.append(
                        await self._telegram.send_text(
                            self._chat_id,
                            conversation.telegram_topic_id,
                            fallback,
                            reply_to_message_id=reply_id if index == 0 else None,
                        )
                    )

            if remaining_text:
                sent_ids.append(
                    await self._telegram.send_text(
                        self._chat_id,
                        conversation.telegram_topic_id,
                        remaining_text,
                        reply_to_message_id=reply_id,
                    )
                )

            if not sent_ids:
                raise RuntimeError("inbound event produced no Telegram messages")

            self._db.store_message_copy(
                conversation.id,
                event.message_id,
                sent_ids[0],
                "inbound",
            )
            self._db.finish_event(event.platform, event.event_id)
            return True
        except Exception as exc:
            self._db.fail_event(event.platform, event.event_id, str(exc))
            raise

    async def handle_telegram_update(self, update: dict[str, object]) -> None:
        update_id = update.get("update_id")
        if not isinstance(update_id, int):
            return
        event_id = str(update_id)
        if not self._db.claim_event("telegram", event_id):
            return

        try:
            raw_message = update.get("message") or update.get("edited_message")
            if not isinstance(raw_message, dict):
                self._db.finish_event("telegram", event_id)
                return
            message = cast(dict[str, object], raw_message)
            if not self._is_authorized_message(message):
                _LOGGER.warning("Rejected unauthorized Telegram update %s", update_id)
                self._db.finish_event("telegram", event_id)
                return

            topic_id = message.get("message_thread_id")
            if not isinstance(topic_id, int):
                self._db.finish_event("telegram", event_id)
                return
            conversation = self._db.get_conversation_by_topic(topic_id)
            if conversation is None:
                await self._handle_unmapped_topic(message, topic_id)
                self._db.finish_event("telegram", event_id)
                return

            text_value = message.get("text") or message.get("caption")
            text = text_value.strip() if isinstance(text_value, str) else None
            if text and text.startswith("/"):
                await self._handle_command(conversation, topic_id, text)
                self._db.finish_event("telegram", event_id)
                return

            image = await self._telegram.download_message_image(message)
            if text is None and image is None:
                self._db.finish_event("telegram", event_id)
                return

            reply_to_external = self._resolve_outbound_reply(conversation, message)
            delivery = await self._adapters.send(
                conversation.platform,
                OutboundMessage(
                    idempotency_key=f"telegram:{update_id}",
                    conversation_id=conversation.external_chat_id,
                    text=text,
                    reply_to_message_id=reply_to_external,
                    image=image.content if image else None,
                    image_filename=image.filename if image else None,
                    image_mime_type=image.mime_type if image else None,
                ),
            )
            telegram_message_id = message.get("message_id")
            if isinstance(telegram_message_id, int):
                self._db.store_message_copy(
                    conversation.id,
                    delivery.message_id,
                    telegram_message_id,
                    "outbound",
                )
            self._db.finish_event("telegram", event_id)
        except Exception as exc:
            self._db.fail_event("telegram", event_id, str(exc))
            raise

    async def _resolve_conversation(self, event: InboundEvent) -> Conversation:
        conversation = self._db.get_conversation(event.platform, event.conversation_id)
        if conversation is not None:
            if conversation.display_name != event.display_name:
                topic_name = self._topic_name(event.platform, event.display_name)
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
                self._topic_name(event.platform, event.display_name),
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
        conversation: Conversation,
        topic_id: int,
        text: str,
    ) -> None:
        command, _, argument = text.partition(" ")
        command = command.split("@", 1)[0].lower()
        if command == "/status":
            status = await self._adapters.status(conversation.platform)
            await self._telegram.send_text(
                self._chat_id,
                topic_id,
                f"{conversation.platform}: {status}",
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
            await self._telegram.send_text(
                self._chat_id,
                topic_id,
                "Команды: /status, /rename <имя>, /close",
            )

    async def _handle_unmapped_topic(
        self,
        message: dict[str, object],
        topic_id: int,
    ) -> None:
        text = message.get("text")
        if text == "/status":
            discord, steam = await asyncio.gather(
                self._adapters.status("discord"),
                self._adapters.status("steam"),
            )
            await self._telegram.send_text(
                self._chat_id,
                topic_id,
                f"discord: {discord}\nsteam: {steam}",
            )

    @staticmethod
    def _topic_name(platform: str, display_name: str) -> str:
        return f"{_PLATFORM_ICON[platform]} · {display_name}"[:128]
