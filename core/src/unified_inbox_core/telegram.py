from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import cast

import aiohttp

from unified_inbox_core.errors import DeliveryError


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def split_utf16(text: str, limit: int) -> list[str]:
    if limit <= 0:
        raise ValueError("text limit must be positive")
    if not text:
        return []
    chunks: list[str] = []
    current: list[str] = []
    current_units = 0
    for character in text:
        units = utf16_length(character)
        if units > limit:
            raise ValueError("a single character exceeds the text limit")
        if current and current_units + units > limit:
            chunks.append("".join(current))
            current = []
            current_units = 0
        current.append(character)
        current_units += units
    if current:
        chunks.append("".join(current))
    if "".join(chunks) != text:
        raise RuntimeError("text splitting changed message content")
    return chunks


class TelegramError(DeliveryError):
    """Raised for a failed Telegram Bot API call."""


@dataclass(frozen=True, slots=True)
class TelegramImage:
    content: bytes
    filename: str
    mime_type: str


class TelegramClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        token: str,
        max_image_bytes: int,
    ) -> None:
        self._session = session
        self._base_url = f"https://api.telegram.org/bot{token}"
        self._file_url = f"https://api.telegram.org/file/bot{token}"
        self._max_image_bytes = max_image_bytes
        self._send_lock = asyncio.Lock()
        self._rate_tokens = 3.0
        self._rate_updated_at: float | None = None

    async def initialize_polling(self) -> None:
        await self.call("deleteWebhook", {"drop_pending_updates": False})

    async def get_me(self) -> dict[str, object]:
        return await self.call_object("getMe", {})

    async def get_updates(self, offset: int, timeout_seconds: int) -> list[dict[str, object]]:
        result = await self.call(
            "getUpdates",
            {
                "offset": offset,
                "timeout": timeout_seconds,
                "allowed_updates": ["message"],
            },
            timeout_seconds=timeout_seconds + 10,
        )
        if not isinstance(result, list):
            raise TelegramError("getUpdates result is not an array")
        updates: list[dict[str, object]] = []
        typed_result = cast(list[object], result)
        for item in typed_result:
            if isinstance(item, dict):
                updates.append(cast(dict[str, object], item))
        return updates

    async def create_topic(self, chat_id: int, name: str) -> int:
        if utf16_length(name) > 128:
            raise ValueError("Telegram topic name exceeds 128 UTF-16 units")
        result = await self.call_object(
            "createForumTopic",
            {"chat_id": chat_id, "name": name},
        )
        topic_id = result.get("message_thread_id")
        if not isinstance(topic_id, int):
            raise TelegramError("createForumTopic returned no message_thread_id")
        return topic_id

    async def edit_topic(self, chat_id: int, topic_id: int, name: str) -> None:
        if utf16_length(name) > 128:
            raise ValueError("Telegram topic name exceeds 128 UTF-16 units")
        await self.call(
            "editForumTopic",
            {"chat_id": chat_id, "message_thread_id": topic_id, "name": name},
        )

    async def close_topic(self, chat_id: int, topic_id: int) -> None:
        await self.call(
            "closeForumTopic",
            {"chat_id": chat_id, "message_thread_id": topic_id},
        )

    async def send_text(
        self,
        chat_id: int,
        topic_id: int,
        text: str,
        reply_to_message_id: int | None = None,
    ) -> int:
        if not text or utf16_length(text) > 4096:
            raise ValueError("Telegram text must contain 1-4096 UTF-16 units")
        payload: dict[str, object] = {
            "chat_id": chat_id,
            "message_thread_id": topic_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if reply_to_message_id is not None:
            payload["reply_parameters"] = {
                "message_id": reply_to_message_id,
                "allow_sending_without_reply": True,
            }
        await self._wait_for_send_slot()
        result = await self.call("sendMessage", payload)
        return self._message_id(result)

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
        if caption is not None and utf16_length(caption) > 1024:
            raise ValueError("Telegram caption exceeds 1024 UTF-16 units")
        form = aiohttp.FormData()
        form.add_field("chat_id", str(chat_id))
        form.add_field("message_thread_id", str(topic_id))
        if caption:
            form.add_field("caption", caption)
        if reply_to_message_id is not None:
            form.add_field(
                "reply_parameters",
                json.dumps(
                    {
                        "message_id": reply_to_message_id,
                        "allow_sending_without_reply": True,
                    }
                ),
            )
        form.add_field("photo", image, filename=filename, content_type=mime_type)
        await self._wait_for_send_slot()
        result = await self._call_form("sendPhoto", form, timeout_seconds=90)
        return self._message_id(result)

    async def set_reaction(self, chat_id: int, message_id: int, emoji: str) -> None:
        await self._wait_for_send_slot()
        await self.call(
            "setMessageReaction",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "reaction": [{"type": "emoji", "emoji": emoji}],
            },
        )

    async def download_message_image(self, message: dict[str, object]) -> TelegramImage | None:
        file_id: str | None = None
        filename = "telegram-image.jpg"
        mime_type = "image/jpeg"

        photos = message.get("photo")
        if isinstance(photos, list) and photos:
            typed_photos = cast(list[object], photos)
            largest = typed_photos[-1]
            if isinstance(largest, dict):
                typed_largest = cast(dict[str, object], largest)
                largest_file_id = typed_largest.get("file_id")
                if isinstance(largest_file_id, str):
                    file_id = largest_file_id

        document = message.get("document")
        if file_id is None and isinstance(document, dict):
            typed_document = cast(dict[str, object], document)
            document_type = typed_document.get("mime_type")
            document_file_id = typed_document.get("file_id")
            if (
                isinstance(document_type, str)
                and document_type.startswith("image/")
                and isinstance(document_file_id, str)
            ):
                file_id = document_file_id
                mime_type = document_type
                document_name = typed_document.get("file_name")
                if isinstance(document_name, str) and document_name:
                    filename = document_name

        if file_id is None:
            return None

        file_info = await self.call_object("getFile", {"file_id": file_id})
        file_path = file_info.get("file_path")
        file_size = file_info.get("file_size")
        if not isinstance(file_path, str):
            raise TelegramError("getFile returned no file_path")
        if isinstance(file_size, int) and file_size > self._max_image_bytes:
            raise TelegramError(
                f"Telegram image exceeds {self._max_image_bytes} bytes",
                retryable=False,
            )

        content = await self._download_file(file_path)
        return TelegramImage(content=content, filename=filename, mime_type=mime_type)

    async def call_object(
        self,
        method: str,
        payload: dict[str, object],
        timeout_seconds: int = 30,
    ) -> dict[str, object]:
        result = await self.call(method, payload, timeout_seconds)
        if not isinstance(result, dict):
            raise TelegramError(f"Telegram {method} result is not an object")
        return cast(dict[str, object], result)

    async def call(
        self,
        method: str,
        payload: dict[str, object],
        timeout_seconds: int = 30,
    ) -> object:
        timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        try:
            async with self._session.post(
                f"{self._base_url}/{method}",
                json=payload,
                timeout=timeout,
            ) as response:
                return await self._parse_response(method, response)
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise TelegramError(f"Telegram {method} request failed") from exc

    async def _call_form(
        self,
        method: str,
        form: aiohttp.FormData,
        timeout_seconds: int,
    ) -> object:
        try:
            async with self._session.post(
                f"{self._base_url}/{method}",
                data=form,
                timeout=aiohttp.ClientTimeout(total=timeout_seconds),
            ) as response:
                return await self._parse_response(method, response)
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise TelegramError(f"Telegram {method} request failed") from exc

    async def _parse_response(
        self,
        method: str,
        response: aiohttp.ClientResponse,
    ) -> object:
        try:
            raw_object: object = await response.json()
        except (aiohttp.ContentTypeError, json.JSONDecodeError) as exc:
            raise TelegramError(
                f"Telegram {method} returned HTTP {response.status} with invalid JSON",
                retryable=response.status >= 500,
            ) from exc
        if not isinstance(raw_object, dict):
            raise TelegramError(f"Telegram {method} returned an invalid response")
        raw = cast(dict[str, object], raw_object)
        if raw.get("ok") is not True:
            description = raw.get("description")
            error_code = raw.get("error_code")
            status = error_code if isinstance(error_code, int) else response.status
            retry_after: float | None = None
            parameters = raw.get("parameters")
            if isinstance(parameters, dict):
                typed_parameters = cast(dict[str, object], parameters)
                raw_retry_after = typed_parameters.get("retry_after")
                if isinstance(raw_retry_after, int | float):
                    retry_after = float(raw_retry_after)
            raise TelegramError(
                f"Telegram {method} failed: {description}",
                retryable=status == 429 or status >= 500,
                retry_after=retry_after,
            )
        if "result" not in raw:
            raise TelegramError(f"Telegram {method} returned no result")
        return raw["result"]

    async def _download_file(self, file_path: str) -> bytes:
        try:
            async with self._session.get(
                f"{self._file_url}/{file_path}",
                timeout=aiohttp.ClientTimeout(total=60),
            ) as response:
                if response.status != 200:
                    raise TelegramError(
                        f"Telegram file download returned HTTP {response.status}",
                        retryable=response.status >= 500,
                    )
                if (
                    response.content_length is not None
                    and response.content_length > self._max_image_bytes
                ):
                    raise TelegramError(
                        f"Telegram image exceeds {self._max_image_bytes} bytes",
                        retryable=False,
                    )
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.content.iter_chunked(64 * 1024):
                    total += len(chunk)
                    if total > self._max_image_bytes:
                        raise TelegramError(
                            f"Telegram image exceeds {self._max_image_bytes} bytes",
                            retryable=False,
                        )
                    chunks.append(chunk)
                return b"".join(chunks)
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise TelegramError("Telegram file download failed") from exc

    async def _wait_for_send_slot(self) -> None:
        refill_rate = 20.0 / 60.0
        capacity = 3.0
        async with self._send_lock:
            loop = asyncio.get_running_loop()
            while True:
                now = loop.time()
                if self._rate_updated_at is None:
                    self._rate_updated_at = now
                elapsed = max(0.0, now - self._rate_updated_at)
                self._rate_tokens = min(capacity, self._rate_tokens + elapsed * refill_rate)
                self._rate_updated_at = now
                if self._rate_tokens >= 1.0:
                    self._rate_tokens -= 1.0
                    return
                await asyncio.sleep((1.0 - self._rate_tokens) / refill_rate)

    @staticmethod
    def _message_id(result: object) -> int:
        if not isinstance(result, dict):
            raise TelegramError("Telegram send method returned an invalid result")
        typed_result = cast(dict[str, object], result)
        message_id = typed_result.get("message_id")
        if not isinstance(message_id, int):
            raise TelegramError("Telegram send method returned no message_id")
        return message_id
