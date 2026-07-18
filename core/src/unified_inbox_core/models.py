from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, cast

Platform = Literal["discord", "steam"]
Direction = Literal["inbound", "outbound_native"]
PresenceStatus = Literal["online", "idle", "busy", "offline"]
JobSource = Literal["discord", "steam", "telegram"]
IngressKind = Literal["external_event", "telegram_update"]
JobKind = Literal["route_external_event", "route_telegram_update"]
JobState = Literal["pending", "leased", "succeeded", "failed"]


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

    def to_mapping(self) -> dict[str, object]:
        return {
            "url": self.url,
            "filename": self.filename,
            "mime_type": self.mime_type,
        }


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

    def to_mapping(self) -> dict[str, object]:
        return {
            "platform": self.platform,
            "event_id": self.event_id,
            "conversation_id": self.conversation_id,
            "display_name": self.display_name,
            "sender_id": self.sender_id,
            "sender_name": self.sender_name,
            "message_id": self.message_id,
            "text": self.text,
            "reply_to_message_id": self.reply_to_message_id,
            "attachments": [attachment.to_mapping() for attachment in self.attachments],
            "direction": self.direction,
        }


@dataclass(frozen=True, slots=True)
class PresenceEvent:
    platform: Platform
    event_id: str
    conversation_id: str
    display_name: str
    status: PresenceStatus

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> PresenceEvent:
        platform_value = _required_string(data, "platform")
        if platform_value not in ("discord", "steam"):
            raise ValueError("platform must be discord or steam")
        status_value = _required_string(data, "status")
        if status_value not in ("online", "idle", "busy", "offline"):
            raise ValueError("status must be online, idle, busy, or offline")
        return cls(
            platform=platform_value,
            event_id=_required_string(data, "event_id"),
            conversation_id=_required_string(data, "conversation_id"),
            display_name=_required_string(data, "display_name"),
            status=status_value,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "kind": "presence",
            "platform": self.platform,
            "event_id": self.event_id,
            "conversation_id": self.conversation_id,
            "display_name": self.display_name,
            "status": self.status,
        }


ExternalEvent = InboundEvent | PresenceEvent


def external_event_from_mapping(data: Mapping[str, object]) -> ExternalEvent:
    kind = data.get("kind", "message")
    if kind == "message":
        return InboundEvent.from_mapping(data)
    if kind == "presence":
        return PresenceEvent.from_mapping(data)
    raise ValueError("kind must be message or presence")


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


@dataclass(frozen=True, slots=True)
class EnqueueResult:
    job_id: int | None
    created: bool
    state: JobState | Literal["legacy_succeeded"]


@dataclass(frozen=True, slots=True)
class DeliveryJob:
    id: int
    source: JobSource
    event_id: str
    kind: JobKind
    conversation_key: str
    payload_json: str
    telegram_message_id: int | None
    state: JobState
    attempt_count: int
    available_at: float
    lease_expires_at: float | None
    last_error: str | None
    created_at: float
    updated_at: float


@dataclass(frozen=True, slots=True)
class FailureSummary:
    job_id: int
    source: JobSource
    event_id: str
    conversation_key: str
    attempt_count: int
    error: str
    created_at: float
    updated_at: float


@dataclass(frozen=True, slots=True)
class LegacyFailure:
    source: JobSource
    event_id: str
    error: str
    updated_at: str
