from typing import cast

import aiohttp
import pytest

from unified_inbox_core.telegram import (
    TelegramClient,
    TelegramError,
    split_utf16,
    utf16_length,
)


class FakeResponse:
    def __init__(self, payload: object, status: int = 200) -> None:
        self.payload = payload
        self.status = status

    async def json(self) -> object:
        return self.payload


class InspectableTelegramClient(TelegramClient):
    async def parse_response(self, response: aiohttp.ClientResponse) -> object:
        return await self._parse_response("sendMessage", response)


class CapturingTelegramClient(TelegramClient):
    def __init__(self) -> None:
        super().__init__(cast(aiohttp.ClientSession, object()), "token", 1024)
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def call(
        self,
        method: str,
        payload: dict[str, object],
        timeout_seconds: int = 30,
    ) -> object:
        self.calls.append((method, payload))
        return {"message_id": 10}


@pytest.mark.asyncio
async def test_boolean_bot_api_results_are_accepted() -> None:
    client = InspectableTelegramClient(cast(aiohttp.ClientSession, object()), "token", 1024)
    response = cast(aiohttp.ClientResponse, FakeResponse({"ok": True, "result": True}))

    assert await client.parse_response(response) is True


@pytest.mark.asyncio
async def test_retry_after_is_preserved() -> None:
    client = InspectableTelegramClient(cast(aiohttp.ClientSession, object()), "token", 1024)
    response = cast(
        aiohttp.ClientResponse,
        FakeResponse(
            {
                "ok": False,
                "error_code": 429,
                "description": "Too Many Requests",
                "parameters": {"retry_after": 17},
            },
            status=429,
        ),
    )

    with pytest.raises(TelegramError) as caught:
        await client.parse_response(response)

    assert caught.value.retryable is True
    assert caught.value.retry_after == 17


@pytest.mark.asyncio
async def test_reply_allows_missing_target() -> None:
    client = CapturingTelegramClient()

    await client.send_text(-100123, 77, "hello", reply_to_message_id=42)

    _, payload = client.calls[0]
    assert payload["reply_parameters"] == {
        "message_id": 42,
        "allow_sending_without_reply": True,
    }


def test_utf16_split_preserves_astral_unicode_without_overflow() -> None:
    text = "🙂" * 2500

    chunks = split_utf16(text, 4096)

    assert "".join(chunks) == text
    assert all(utf16_length(chunk) <= 4096 for chunk in chunks)
