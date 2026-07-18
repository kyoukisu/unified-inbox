from __future__ import annotations

from pathlib import Path
from typing import cast

import aiohttp
import pytest

from unified_inbox_core.adapter import AdapterClient, AdapterDelivery
from unified_inbox_core.db import Database
from unified_inbox_core.delivery import DeliveryWorker
from unified_inbox_core.models import DeliveryJob, InboundEvent, OutboundMessage, Platform
from unified_inbox_core.router import Router
from unified_inbox_core.telegram import TelegramClient, TelegramError, TelegramImage


class FakeTelegram:
    def __init__(self) -> None:
        self.created_topics: list[str] = []
        self.sent_text: list[tuple[int, str]] = []
        self.sent_photos: list[tuple[int, str | None]] = []
        self.reactions: list[tuple[int, str]] = []

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
        return 500 + len(self.sent_text) + len(self.sent_photos)

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
        self.sent_photos.append((topic_id, caption))
        return 500 + len(self.sent_text) + len(self.sent_photos)

    async def set_reaction(self, chat_id: int, message_id: int, emoji: str) -> None:
        self.reactions.append((message_id, emoji))

    async def download_message_image(
        self,
        message: dict[str, object],
    ) -> TelegramImage | None:
        return None


class FailingSecondChunkTelegram(FakeTelegram):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0
        self.failed_once = False

    async def send_text(
        self,
        chat_id: int,
        topic_id: int,
        text: str,
        reply_to_message_id: int | None = None,
    ) -> int:
        self.calls += 1
        if self.calls == 2 and not self.failed_once:
            self.failed_once = True
            raise TelegramError("temporary outage", retry_after=0)
        return await super().send_text(
            chat_id,
            topic_id,
            text,
            reply_to_message_id,
        )


class FakeAdapters:
    def __init__(self) -> None:
        self.sent: list[tuple[Platform, OutboundMessage]] = []

    async def send(self, platform: Platform, message: OutboundMessage) -> AdapterDelivery:
        self.sent.append((platform, message))
        return AdapterDelivery(message_id=f"external-out-{len(self.sent)}")

    async def status(self, platform: Platform) -> dict[str, object]:
        return {"ok": True, "platform": platform}


async def process_next(db: Database, router: Router) -> DeliveryJob:
    job = db.claim_next_job(300)
    assert job is not None
    await router.process_job(job)
    db.complete_job(job)
    return job


def inbound_event(**overrides: object) -> InboundEvent:
    payload: dict[str, object] = {
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
    payload.update(overrides)
    return InboundEvent.from_mapping(payload)


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
        first = router.enqueue_inbound(inbound_event())
        duplicate = router.enqueue_inbound(inbound_event())
        assert first.created is True
        assert duplicate.created is False
        await process_next(db, router)

    assert telegram.created_topics == ["🎮 Steam · Alice"]
    assert telegram.sent_text == [(77, "hello")]
    conversation = db.get_conversation("steam", "steam-alice")
    assert conversation is not None
    assert db.telegram_message_for_external(conversation.id, "1700000000:0") == 501
    db.close()


@pytest.mark.asyncio
async def test_native_outbound_uses_outbox_only_for_persisted_conversation(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "bridge.sqlite3")
    conversation = db.create_conversation("discord", "dm-123", "Bob", 77)
    inbox = FakeTelegram()
    outbox = FakeTelegram()
    adapters = FakeAdapters()
    async with aiohttp.ClientSession() as session:
        router = Router(
            db,
            cast(TelegramClient, inbox),
            cast(AdapterClient, adapters),
            session,
            -100123,
            999,
            1024,
            cast(TelegramClient, outbox),
        )
        event = inbound_event(
            platform="discord",
            event_id="self-message-1",
            conversation_id="dm-123",
            display_name="Bob",
            sender_id="me",
            sender_name="You",
            message_id="self-message-1",
            text="sent from Windows",
            direction="outbound_native",
        )
        router.enqueue_inbound(event)
        await process_next(db, router)

    assert inbox.sent_text == []
    assert outbox.sent_text == [(77, "sent from Windows")]
    assert db.telegram_message_for_external(conversation.id, "self-message-1") == 501
    db.close()


@pytest.mark.asyncio
async def test_native_outbound_creates_persisted_native_conversation(tmp_path: Path) -> None:
    db = Database(tmp_path / "bridge.sqlite3")
    inbox = FakeTelegram()
    outbox = FakeTelegram()
    adapters = FakeAdapters()
    async with aiohttp.ClientSession() as session:
        router = Router(
            db,
            cast(TelegramClient, inbox),
            cast(AdapterClient, adapters),
            session,
            -100123,
            999,
            1024,
            cast(TelegramClient, outbox),
        )
        event = inbound_event(
            event_id="unknown-self-message",
            conversation_id="unknown-chat",
            display_name="Unknown",
            sender_id="me",
            sender_name="You",
            message_id="unknown-self-message",
            text="started from native client",
            direction="outbound_native",
        )
        router.enqueue_inbound(event)
        await process_next(db, router)

    assert db.get_conversation("steam", "unknown-chat") is not None
    assert inbox.created_topics == ["🎮 Steam · Unknown"]
    assert outbox.sent_text == [(77, "started from native client")]
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
        router.enqueue_telegram_update(update)
        await process_next(db, router)

    assert len(adapters.sent) == 1
    platform, outbound = adapters.sent[0]
    assert platform == "discord"
    assert outbound.conversation_id == "dm-123"
    assert outbound.text == "reply from Telegram"
    assert outbound.idempotency_key == "telegram:400"
    assert db.get_state_int("telegram_offset", 0) == 401
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
        router.enqueue_telegram_update(
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
        await process_next(db, router)

    assert adapters.sent == []
    db.close()


@pytest.mark.asyncio
async def test_long_inbound_text_is_split_without_loss(tmp_path: Path) -> None:
    db = Database(tmp_path / "bridge.sqlite3")
    telegram = FakeTelegram()
    adapters = FakeAdapters()
    text = "🙂" * 2500
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
        router.enqueue_inbound(inbound_event(text=text))
        await process_next(db, router)

    assert len(telegram.sent_text) == 2
    assert "".join(chunk for _, chunk in telegram.sent_text) == text
    db.close()


@pytest.mark.asyncio
async def test_retry_resumes_after_completed_text_part_without_duplicate(tmp_path: Path) -> None:
    db = Database(tmp_path / "bridge.sqlite3")
    telegram = FailingSecondChunkTelegram()
    adapters = FakeAdapters()
    text = "x" * 5000
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
        router.enqueue_inbound(inbound_event(text=text))
        worker = DeliveryWorker(db, router)

        assert await worker.run_once() is True
        assert db.job_counts()["pending"] == 1
        assert await worker.run_once() is True

    assert "".join(chunk for _, chunk in telegram.sent_text) == text
    assert len(telegram.sent_text) == 2
    assert telegram.calls == 3
    assert db.job_counts()["succeeded"] == 1
    db.close()


@pytest.mark.asyncio
async def test_long_telegram_text_is_split_for_discord(tmp_path: Path) -> None:
    db = Database(tmp_path / "bridge.sqlite3")
    db.create_conversation("discord", "dm-123", "Bob", 77)
    telegram = FakeTelegram()
    adapters = FakeAdapters()
    text = "x" * 4001
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
        router.enqueue_telegram_update(
            {
                "update_id": 402,
                "message": {
                    "message_id": 90,
                    "message_thread_id": 77,
                    "chat": {"id": -100123},
                    "from": {"id": 999},
                    "text": text,
                },
            }
        )
        await process_next(db, router)

    assert [len(message.text or "") for _, message in adapters.sent] == [2000, 2000, 1]
    assert "".join(message.text or "" for _, message in adapters.sent) == text
    assert [message.idempotency_key for _, message in adapters.sent] == [
        "telegram:402",
        "telegram:402:part:1",
        "telegram:402:part:2",
    ]
    db.close()
