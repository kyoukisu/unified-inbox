import discord

from discord_adapter.client import (
    discord_direct_image_attachment,
    discord_embed_attachments,
    discord_nonce_for_idempotency_key,
    discord_nonce_value,
    discord_text_without_embedded_image,
    normalize_discord_presence,
)


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
