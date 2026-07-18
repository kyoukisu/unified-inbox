import pytest

from unified_inbox_core.media import is_allowed_media_url
from unified_inbox_core.models import InboundEvent


def test_inbound_event_requires_content() -> None:
    with pytest.raises(ValueError, match="text or at least one attachment"):
        InboundEvent.from_mapping(
            {
                "platform": "steam",
                "event_id": "event-1",
                "conversation_id": "chat-1",
                "display_name": "Alice",
                "sender_id": "alice",
                "sender_name": "Alice",
                "message_id": "message-1",
                "text": None,
                "attachments": [],
            }
        )


def test_inbound_event_rejects_unknown_direction() -> None:
    with pytest.raises(ValueError, match="direction must be"):
        InboundEvent.from_mapping(
            {
                "platform": "discord",
                "event_id": "event-2",
                "conversation_id": "chat-2",
                "display_name": "Bob",
                "sender_id": "bob",
                "sender_name": "Bob",
                "message_id": "message-2",
                "text": "hello",
                "attachments": [],
                "direction": "sideways",
            }
        )


def test_media_url_allowlist_blocks_credentials_ports_and_lookalikes() -> None:
    assert is_allowed_media_url(
        "discord",
        "https://cdn.discordapp.com/attachments/1/2/image.png",
    )
    assert is_allowed_media_url(
        "steam",
        "https://images.akamai.steamusercontent.com/ugc/image.jpg",
    )
    assert is_allowed_media_url(
        "discord",
        "https://i.gyazo.com/4bb631b00ba6ab9b2fd7a736cba31451.png",
    )
    assert is_allowed_media_url(
        "discord",
        "https://media.tenor.com/example/running-cat.mp4",
    )

    assert not is_allowed_media_url("discord", "http://cdn.discordapp.com/image.png")
    assert not is_allowed_media_url("discord", "https://discordapp.com.evil.test/image.png")
    assert not is_allowed_media_url("discord", "https://i.gyazo.com.evil.test/image.png")
    assert not is_allowed_media_url("discord", "https://media.tenor.com.evil.test/image.gif")
    assert not is_allowed_media_url("discord", "https://user@cdn.discordapp.com/image.png")
    assert not is_allowed_media_url("steam", "https://localhost:8080/private")
