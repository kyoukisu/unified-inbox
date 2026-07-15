from __future__ import annotations

import json
from dataclasses import dataclass
from typing import cast

import aiohttp


class TelegramError(RuntimeError):
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
        result = await self.call_object(
            "createForumTopic",
            {"chat_id": chat_id, "name": name[:128]},
        )
        topic_id = result.get("message_thread_id")
        if not isinstance(topic_id, int):
            raise TelegramError("createForumTopic returned no message_thread_id")
        return topic_id

    async def edit_topic(self, chat_id: int, topic_id: int, name: str) -> None:
        await self.call(
            "editForumTopic",
            {"chat_id": chat_id, "message_thread_id": topic_id, "name": name[:128]},
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
        payload: dict[str, object] = {
            "chat_id": chat_id,
            "message_thread_id": topic_id,
            "text": text[:4096],
            "disable_web_page_preview": True,
        }
        if reply_to_message_id is not None:
            payload["reply_parameters"] = {"message_id": reply_to_message_id}
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
        form = aiohttp.FormData()
        form.add_field("chat_id", str(chat_id))
        form.add_field("message_thread_id", str(topic_id))
        if caption:
            form.add_field("caption", caption[:1024])
        if reply_to_message_id is not None:
            form.add_field("reply_parameters", json.dumps({"message_id": reply_to_message_id}))
        form.add_field("photo", image, filename=filename, content_type=mime_type)
        result = await self._call_form("sendPhoto", form, timeout_seconds=90)
        return self._message_id(result)

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
            raise TelegramError(f"Telegram image exceeds {self._max_image_bytes} bytes")

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
            raise TelegramError(f"Telegram {method} returned invalid JSON") from exc
        if not isinstance(raw_object, dict):
            raise TelegramError(f"Telegram {method} returned an invalid response")
        raw = cast(dict[str, object], raw_object)
        if raw.get("ok") is not True:
            description = raw.get("description")
            raise TelegramError(f"Telegram {method} failed: {description}")
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
                    raise TelegramError(f"Telegram file download returned HTTP {response.status}")
                if (
                    response.content_length is not None
                    and response.content_length > self._max_image_bytes
                ):
                    raise TelegramError(f"Telegram image exceeds {self._max_image_bytes} bytes")
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.content.iter_chunked(64 * 1024):
                    total += len(chunk)
                    if total > self._max_image_bytes:
                        raise TelegramError(f"Telegram image exceeds {self._max_image_bytes} bytes")
                    chunks.append(chunk)
                return b"".join(chunks)
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise TelegramError("Telegram file download failed") from exc

    @staticmethod
    def _message_id(result: object) -> int:
        if not isinstance(result, dict):
            raise TelegramError("Telegram send method returned an invalid result")
        typed_result = cast(dict[str, object], result)
        message_id = typed_result.get("message_id")
        if not isinstance(message_id, int):
            raise TelegramError("Telegram send method returned no message_id")
        return message_id
