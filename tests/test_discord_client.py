import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import aiohttp
import discord
import pytest

from discord_adapter.client import (
    CoreDeliveryError,
    DiscordBridgeClient,
    discord_direct_image_attachment,
    discord_embed_attachments,
    discord_history_newer_than,
    discord_messages_oldest_first,
    discord_nonce_for_idempotency_key,
    discord_nonce_value,
    discord_text_without_embedded_image,
    is_tenor_view_url,
    normalize_discord_presence,
    wait_for_discord_embed,
)
from discord_adapter.store import AdapterStore


@pytest.mark.asyncio
async def test_waits_for_discord_to_generate_tenor_embed() -> None:
    refreshed = SimpleNamespace(embeds=[object()])

    class FakeChannel:
        def __init__(self) -> None:
            self.calls = 0

        async def fetch_message(self, message_id: int) -> discord.Message:
            assert message_id == 123
            self.calls += 1
            return cast(discord.Message, refreshed)

    channel = FakeChannel()
    message = cast(
        discord.Message,
        SimpleNamespace(
            id=123,
            content="https://tenor.com/view/cat-gif-123",
            embeds=[],
            channel=channel,
        ),
    )

    result = await wait_for_discord_embed(message, delays=(0,))

    assert result is refreshed
    assert channel.calls == 1
    assert is_tenor_view_url("https://tenor.com/view/cat-gif-123")
    assert not is_tenor_view_url("look https://tenor.com/view/cat-gif-123")


def test_discord_messages_are_deduplicated_and_sorted_oldest_first() -> None:
    message_100 = cast(discord.Message, SimpleNamespace(id=100))
    message_200 = cast(discord.Message, SimpleNamespace(id=200))
    duplicate_200 = cast(discord.Message, SimpleNamespace(id=200))
    message_300 = cast(discord.Message, SimpleNamespace(id=300))

    ordered = discord_messages_oldest_first(
        [message_300, message_200],
        [message_100, duplicate_200],
    )

    assert [message.id for message in ordered] == [100, 200, 300]
    assert ordered[1] is duplicate_200


@pytest.mark.asyncio
async def test_discord_history_uses_watermark_or_24_hour_bootstrap() -> None:
    messages = [
        cast(discord.Message, SimpleNamespace(id=100)),
        cast(discord.Message, SimpleNamespace(id=200)),
    ]

    class FakeChannel:
        def __init__(self) -> None:
            self.after_values: list[object] = []

        def history(
            self,
            *,
            limit: int | None,
            after: object,
            oldest_first: bool,
        ) -> AsyncIterator[discord.Message]:
            assert limit is None
            assert oldest_first is True
            self.after_values.append(after)

            async def iterate() -> AsyncIterator[discord.Message]:
                for message in messages:
                    yield message

            return iterate()

    channel = FakeChannel()
    discord_channel = cast(discord.DMChannel, channel)
    bootstrap_after = datetime.now(UTC) - timedelta(hours=24)

    assert await discord_history_newer_than(discord_channel, 99, bootstrap_after) == messages
    watermark_after = channel.after_values[-1]
    assert isinstance(watermark_after, discord.Object)
    assert watermark_after.id == 99

    assert await discord_history_newer_than(discord_channel, None, bootstrap_after) == messages
    assert channel.after_values[-1] is bootstrap_after


@pytest.mark.asyncio
async def test_core_http_400_is_quarantined_without_blocking_spool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = AdapterStore(tmp_path / "discord.sqlite3")
    assert store.enqueue_event({"event_id": "bad"}) is True
    assert store.enqueue_event({"event_id": "good"}) is True
    client = DiscordBridgeClient(
        cast(aiohttp.ClientSession, object()),
        "http://core.invalid",
        "token",
        store,
    )
    delivered = asyncio.Event()

    async def post_to_core(payload: dict[str, object]) -> None:
        if payload["event_id"] == "bad":
            raise CoreDeliveryError(400, "invalid event")
        delivered.set()

    monkeypatch.setattr(client, "_post_to_core", post_to_core)
    client.start_spool()
    spool = client.spool_task
    assert spool is not None
    try:
        await asyncio.wait_for(delivered.wait(), timeout=1)
        assert store.pending_count() == 0
        assert store.dead_letter_count() == 1
    finally:
        spool.cancel()
        await asyncio.gather(spool, return_exceptions=True)
        store.close()


def test_discord_presence_statuses_are_normalized() -> None:
    assert normalize_discord_presence(discord.Status.online) == "online"
    assert normalize_discord_presence(discord.Status.idle) == "idle"
    assert normalize_discord_presence(discord.Status.dnd) == "busy"
    assert normalize_discord_presence(discord.Status.offline) == "offline"
    assert normalize_discord_presence(discord.Status.invisible) == "offline"
    assert normalize_discord_presence("unknown") is None


def test_discord_nonce_fits_signed_int64_and_is_deterministic() -> None:
    failed_key = "telegram:179535368"

    nonce = discord_nonce_for_idempotency_key(failed_key)

    assert nonce == discord_nonce_for_idempotency_key(failed_key)
    assert 0 <= nonce <= (1 << 63) - 1


def test_discord_nonce_retains_key_distinction() -> None:
    assert discord_nonce_for_idempotency_key(
        "telegram:179535367"
    ) != discord_nonce_for_idempotency_key("telegram:179535368")


def test_discord_gateway_string_nonce_is_normalized() -> None:
    assert discord_nonce_value("4949912097577381323") == 4949912097577381323
    assert discord_nonce_value(4949912097577381323) == 4949912097577381323
    assert discord_nonce_value("not-a-nonce") is None


def test_discord_image_embed_becomes_attachment_without_bare_url() -> None:
    source_url = "https://i.gyazo.com/example.png"
    proxy_url = "https://images-ext-1.discordapp.net/external/example/https/i.gyazo.com/example.png"
    displayed_proxy_url = f"{proxy_url}?format=webp&quality=lossless"
    embed = discord.Embed.from_dict(
        {
            "type": "image",
            "url": source_url,
            "thumbnail": {
                "url": source_url,
                "proxy_url": proxy_url,
                "content_type": "image/png",
            },
        }
    )

    attachments, image_urls = discord_embed_attachments([embed], set())

    assert attachments == [
        {
            "url": proxy_url,
            "filename": "example.png",
            "mime_type": "image/png",
        }
    ]
    assert discord_text_without_embedded_image(source_url, image_urls) is None
    assert discord_text_without_embedded_image(displayed_proxy_url, image_urls) is None
    assert (
        discord_text_without_embedded_image(f"look: {source_url}", image_urls)
        == f"look: {source_url}"
    )


def test_discord_gifv_embed_prefers_animated_video() -> None:
    tenor_url = "https://tenor.com/view/running-cat-gif-123"
    video_url = "https://media.tenor.com/running-cat.mp4"
    proxy_url = "https://images-ext-1.discordapp.net/external/example/running-cat.mp4"
    embed = discord.Embed.from_dict(
        {
            "type": "gifv",
            "url": tenor_url,
            "thumbnail": {
                "url": "https://media.tenor.com/running-cat.gif",
                "proxy_url": "https://images-ext-1.discordapp.net/running-cat.gif",
                "content_type": "image/gif",
            },
            "video": {
                "url": video_url,
                "proxy_url": proxy_url,
                "content_type": "video/mp4",
            },
        }
    )

    attachments, image_urls = discord_embed_attachments([embed], set())

    assert attachments == [
        {
            "url": video_url,
            "filename": "running-cat.mp4",
            "mime_type": "video/mp4",
        }
    ]
    assert discord_text_without_embedded_image(tenor_url, image_urls) is None


def test_discord_direct_image_url_works_before_embed_is_ready() -> None:
    image_url = (
        "https://images-ext-1.discordapp.net/external/example/https/i.gyazo.com/example.png"
        "?format=webp&quality=lossless"
    )

    attachment, image_urls = discord_direct_image_attachment(image_url)

    assert attachment == {
        "url": image_url,
        "filename": "example.webp",
        "mime_type": "image/webp",
    }
    assert discord_text_without_embedded_image(image_url, image_urls) is None
    assert discord_direct_image_attachment("https://example.com/not-an-image")[0] is None
    assert discord_direct_image_attachment("https://example.com:bad/image.png")[0] is None
