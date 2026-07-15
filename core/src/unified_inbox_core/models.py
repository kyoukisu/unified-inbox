from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, cast

Platform = Literal["discord", "steam"]
Direction = Literal["inbound", "outbound_native"]


def _required_string(data: Mapping[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def _optional_string(data: Mapping[str, object], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string or null")
    stripped = value.strip()
    return stripped or None


@dataclass(frozen=True, slots=True)
class Attachment:
    url: str
    filename: str
    mime_type: str

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> Attachment:
        url = _required_string(data, "url")
        if not url.startswith("https://"):
            raise ValueError("attachment URL must use HTTPS")
        return cls(
            url=url,
            filename=_required_string(data, "filename"),
            mime_type=_required_string(data, "mime_type"),
        )


@dataclass(frozen=True, slots=True)
class InboundEvent:
    platform: Platform
    event_id: str
    conversation_id: str
    display_name: str
    sender_id: str
    sender_name: str
    message_id: str
    text: str | None
    reply_to_message_id: str | None
    attachments: tuple[Attachment, ...]
    direction: Direction

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> InboundEvent:
        platform_value = _required_string(data, "platform")
        if platform_value not in ("discord", "steam"):
            raise ValueError("platform must be discord or steam")

        attachments_value = data.get("attachments", [])
        if not isinstance(attachments_value, Sequence) or isinstance(attachments_value, str):
            raise ValueError("attachments must be an array")

        attachments: list[Attachment] = []
        typed_attachments = cast(Sequence[object], attachments_value)
        for value in typed_attachments:
            if not isinstance(value, Mapping):
                raise ValueError("each attachment must be an object")
            attachments.append(Attachment.from_mapping(cast(Mapping[str, object], value)))

        text = _optional_string(data, "text")
        if text is None and not attachments:
            raise ValueError("event must contain text or at least one attachment")

        direction_value = data.get("direction", "inbound")
        if direction_value not in ("inbound", "outbound_native"):
            raise ValueError("direction must be inbound or outbound_native")

        return cls(
            platform=platform_value,
            event_id=_required_string(data, "event_id"),
            conversation_id=_required_string(data, "conversation_id"),
            display_name=_required_string(data, "display_name"),
            sender_id=_required_string(data, "sender_id"),
            sender_name=_required_string(data, "sender_name"),
            message_id=_required_string(data, "message_id"),
            text=text,
            reply_to_message_id=_optional_string(data, "reply_to_message_id"),
            attachments=tuple(attachments),
            direction=direction_value,
        )


@dataclass(frozen=True, slots=True)
class Conversation:
    id: int
    platform: Platform
    external_chat_id: str
    display_name: str
    telegram_topic_id: int


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    idempotency_key: str
    conversation_id: str
    text: str | None
    reply_to_message_id: str | None
    image: bytes | None
    image_filename: str | None
    image_mime_type: str | None
