from __future__ import annotations

from pathlib import Path
from typing import cast

import aiohttp
import pytest

from unified_inbox_core.adapter import AdapterClient, AdapterDelivery
from unified_inbox_core.db import Database
from unified_inbox_core.models import InboundEvent, OutboundMessage, Platform
from unified_inbox_core.router import Router
from unified_inbox_core.telegram import TelegramClient, TelegramImage


class FakeTelegram:
    def __init__(self) -> None:
        self.created_topics: list[str] = []
        self.sent_text: list[tuple[int, str]] = []

    async def create_topic(self, chat_id: int, name: str) -> int:
        assert chat_id == -100123
        self.created_topics.append(name)
        return 77

    async def edit_topic(self, chat_id: int, topic_id: int, name: str) -> None:
        return None

    async def close_topic(self, chat_id: int, topic_id: int) -> None:
        return None

    async def send_text(
        self,
        chat_id: int,
        topic_id: int,
        text: str,
        reply_to_message_id: int | None = None,
    ) -> int:
        self.sent_text.append((topic_id, text))
        return 501 + len(self.sent_text)

    async def send_photo(
        self,
        chat_id: int,
        topic_id: int,
        image: bytes,
        filename: str,
        mime_type: str,
        caption: str | None = None,
        reply_to_message_id: int | None = None,
    ) -> int:
        raise AssertionError("photo path is not expected in this test")

    async def download_message_image(
        self,
        message: dict[str, object],
    ) -> TelegramImage | None:
        return None


class FakeAdapters:
    def __init__(self) -> None:
        self.sent: list[tuple[Platform, OutboundMessage]] = []

    async def send(self, platform: Platform, message: OutboundMessage) -> AdapterDelivery:
        self.sent.append((platform, message))
        return AdapterDelivery(message_id="external-out-1")

    async def status(self, platform: Platform) -> dict[str, object]:
        return {"ok": True, "platform": platform}


@pytest.mark.asyncio
async def test_inbound_text_creates_one_topic_and_deduplicates(tmp_path: Path) -> None:
    db = Database(tmp_path / "bridge.sqlite3")
    telegram = FakeTelegram()
    adapters = FakeAdapters()
    async with aiohttp.ClientSession() as session:
        router = Router(
            db,
            cast(TelegramClient, telegram),
            cast(AdapterClient, adapters),
            session,
            -100123,
            999,
            1024,
        )
        event = InboundEvent.from_mapping(
            {
                "platform": "steam",
                "event_id": "event-1",
                "conversation_id": "steam-alice",
                "display_name": "Alice",
                "sender_id": "steam-alice",
                "sender_name": "Alice",
                "message_id": "1700000000:0",
                "text": "hello",
                "attachments": [],
            }
        )

        assert await router.handle_inbound(event) is True
        assert await router.handle_inbound(event) is False

    assert telegram.created_topics == ["🎮 Steam · Alice"]
    assert telegram.sent_text == [(77, "hello")]
    conversation = db.get_conversation("steam", "steam-alice")
    assert conversation is not None
    assert db.telegram_message_for_external(conversation.id, "1700000000:0") == 502
    db.close()


@pytest.mark.asyncio
async def test_authorized_telegram_message_routes_to_external_adapter(tmp_path: Path) -> None:
    db = Database(tmp_path / "bridge.sqlite3")
    db.create_conversation("discord", "dm-123", "Bob", 77)
    telegram = FakeTelegram()
    adapters = FakeAdapters()
    async with aiohttp.ClientSession() as session:
        router = Router(
            db,
            cast(TelegramClient, telegram),
            cast(AdapterClient, adapters),
            session,
            -100123,
            999,
            1024,
        )
        update: dict[str, object] = {
            "update_id": 400,
            "message": {
                "message_id": 88,
                "message_thread_id": 77,
                "chat": {"id": -100123},
                "from": {"id": 999},
                "text": "reply from Telegram",
            },
        }
        await router.handle_telegram_update(update)

    assert len(adapters.sent) == 1
    platform, outbound = adapters.sent[0]
    assert platform == "discord"
    assert outbound.conversation_id == "dm-123"
    assert outbound.text == "reply from Telegram"
    assert outbound.idempotency_key == "telegram:400"
    db.close()


@pytest.mark.asyncio
async def test_unauthorized_telegram_sender_is_rejected(tmp_path: Path) -> None:
    db = Database(tmp_path / "bridge.sqlite3")
    db.create_conversation("steam", "steam-alice", "Alice", 77)
    telegram = FakeTelegram()
    adapters = FakeAdapters()
    async with aiohttp.ClientSession() as session:
        router = Router(
            db,
            cast(TelegramClient, telegram),
            cast(AdapterClient, adapters),
            session,
            -100123,
            999,
            1024,
        )
        await router.handle_telegram_update(
            {
                "update_id": 401,
                "message": {
                    "message_id": 89,
                    "message_thread_id": 77,
                    "chat": {"id": -100123},
                    "from": {"id": 123456},
                    "text": "steal account",
                },
            }
        )

    assert adapters.sent == []
    db.close()
