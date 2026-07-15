from typing import cast

import aiohttp
import pytest

from unified_inbox_core.telegram import TelegramClient


class FakeResponse:
    status = 200

    async def json(self) -> object:
        return {"ok": True, "result": True}


class InspectableTelegramClient(TelegramClient):
    async def parse_response(self, response: aiohttp.ClientResponse) -> object:
        return await self._parse_response("deleteWebhook", response)


@pytest.mark.asyncio
async def test_boolean_bot_api_results_are_accepted() -> None:
    client = InspectableTelegramClient(cast(aiohttp.ClientSession, object()), "token", 1024)
    response = cast(aiohttp.ClientResponse, FakeResponse())

    assert await client.parse_response(response) is True
