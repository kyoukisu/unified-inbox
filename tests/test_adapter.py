import json
from typing import cast

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from unified_inbox_core.adapter import AdapterClient
from unified_inbox_core.models import OutboundMessage


@pytest.mark.asyncio
async def test_adapter_sends_multipart_metadata_as_text() -> None:
    received: dict[str, object] = {}

    async def receive(request: web.Request) -> web.Response:
        form = await request.post()
        metadata = form.get("metadata")
        if not isinstance(metadata, str):
            return web.json_response(
                {"ok": False, "error": "multipart request has no metadata"},
                status=400,
            )
        received.update(cast(dict[str, object], json.loads(metadata)))
        return web.json_response({"ok": True, "message_id": "external-1"})

    application = web.Application()
    application.router.add_post("/v1/messages", receive)
    server = TestServer(application)
    await server.start_server()
    try:
        async with aiohttp.ClientSession() as session:
            client = AdapterClient(
                session,
                "internal-token",
                {"discord": str(server.make_url("/")).rstrip("/")},
            )
            delivery = await client.send(
                "discord",
                OutboundMessage(
                    idempotency_key="telegram:1",
                    conversation_id="dm-1",
                    text="caption",
                    reply_to_message_id=None,
                    image=b"image-bytes",
                    image_filename="image.png",
                    image_mime_type="image/png",
                ),
            )
    finally:
        await server.close()

    assert delivery.message_id == "external-1"
    assert received["idempotency_key"] == "telegram:1"


@pytest.mark.asyncio
async def test_adapter_deletes_external_message() -> None:
    received: dict[str, object] = {}

    async def delete(request: web.Request) -> web.Response:
        received.update(cast(dict[str, object], await request.json()))
        received["message_id"] = request.match_info["message_id"]
        return web.json_response({"ok": True})

    application = web.Application()
    application.router.add_delete("/v1/messages/{message_id}", delete)
    server = TestServer(application)
    await server.start_server()
    try:
        async with aiohttp.ClientSession() as session:
            client = AdapterClient(
                session,
                "internal-token",
                {"discord": str(server.make_url("/")).rstrip("/")},
            )
            await client.delete("discord", "dm-1", "external-1")
    finally:
        await server.close()

    assert received == {"conversation_id": "dm-1", "message_id": "external-1"}
