from __future__ import annotations

import asyncio
import hmac
import json
import logging
from contextlib import suppress
from typing import cast

import aiohttp
from aiohttp import web
from aiohttp.web_request import FileField

from discord_adapter.client import DiscordBridgeClient
from discord_adapter.config import Settings

_LOGGER = logging.getLogger(__name__)


class DiscordAdapterApplication:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._session = aiohttp.ClientSession()
        self._client = DiscordBridgeClient(
            self._session,
            settings.core_url,
            settings.internal_token,
        )
        self._discord_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._discord_task = asyncio.create_task(
            self._client.start(self._settings.discord_token),
            name="discord-client",
        )

    async def close(self) -> None:
        await self._client.close()
        if self._discord_task is not None:
            self._discord_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._discord_task
        await self._session.close()

    def web_application(self) -> web.Application:
        app = web.Application(client_max_size=self._settings.max_image_bytes + 1024 * 1024)
        app.router.add_get("/health", self.health)
        app.router.add_post("/v1/messages", self.send_message)
        return app

    async def health(self, request: web.Request) -> web.Response:
        user = self._client.user
        return web.json_response(
            {
                "ok": user is not None and not self._client.is_closed(),
                "connected": user is not None and not self._client.is_closed(),
                "user_id": str(user.id) if user is not None else None,
            }
        )

    async def send_message(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            raise web.HTTPUnauthorized(text="invalid internal token")
        try:
            metadata, image, image_filename = await self._parse_request(request)
            message_id = await self._client.send_message(
                conversation_id=self._required_string(metadata, "conversation_id"),
                idempotency_key=self._required_string(metadata, "idempotency_key"),
                text=self._optional_string(metadata, "text"),
                reply_to_message_id=self._optional_string(metadata, "reply_to_message_id"),
                image=image,
                image_filename=image_filename,
            )
        except (ValueError, json.JSONDecodeError) as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        except Exception:
            _LOGGER.exception("Discord outbound delivery failed")
            raise web.HTTPBadGateway(text="Discord outbound delivery failed") from None
        return web.json_response({"ok": True, "message_id": message_id})

    async def _parse_request(
        self,
        request: web.Request,
    ) -> tuple[dict[str, object], bytes | None, str | None]:
        if request.content_type == "application/json":
            raw_object: object = await request.json()
            if not isinstance(raw_object, dict):
                raise ValueError("request body must be an object")
            return cast(dict[str, object], raw_object), None, None

        if not request.content_type.startswith("multipart/"):
            raise ValueError("request must be JSON or multipart")
        form = await request.post()
        metadata_field = form.get("metadata")
        if not isinstance(metadata_field, str):
            raise ValueError("multipart request has no metadata")
        metadata_object: object = json.loads(metadata_field)
        if not isinstance(metadata_object, dict):
            raise ValueError("metadata must be an object")
        metadata = cast(dict[str, object], metadata_object)

        image_field = form.get("image")
        if image_field is None:
            return metadata, None, None
        if not isinstance(image_field, FileField):
            raise ValueError("image must be a file")
        image = image_field.file.read(self._settings.max_image_bytes + 1)
        if len(image) > self._settings.max_image_bytes:
            raise ValueError("image is too large")
        return metadata, image, image_field.filename

    def _authorized(self, request: web.Request) -> bool:
        authorization = request.headers.get("Authorization", "")
        prefix = "Bearer "
        return authorization.startswith(prefix) and hmac.compare_digest(
            authorization[len(prefix) :],
            self._settings.internal_token,
        )

    @staticmethod
    def _required_string(data: dict[str, object], key: str) -> str:
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} must be a non-empty string")
        return value.strip()

    @staticmethod
    def _optional_string(data: dict[str, object], key: str) -> str | None:
        value = data.get(key)
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError(f"{key} must be a string or null")
        return value.strip() or None


async def _run() -> None:
    settings = Settings.from_environment()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    adapter = DiscordAdapterApplication(settings)
    runner = web.AppRunner(adapter.web_application(), access_log=None)
    try:
        await adapter.start()
        await runner.setup()
        site = web.TCPSite(runner, settings.bind, settings.port)
        await site.start()
        _LOGGER.info("Discord adapter listening on %s:%s", settings.bind, settings.port)
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        await adapter.close()


def run() -> None:
    asyncio.run(_run())
