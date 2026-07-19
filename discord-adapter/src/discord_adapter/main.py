from __future__ import annotations

import asyncio
import hmac
import json
import logging
from typing import cast

import aiohttp
from aiohttp import web
from aiohttp.web_request import FileField

from discord_adapter.client import DiscordBridgeClient
from discord_adapter.config import Settings
from discord_adapter.store import AdapterStore

_LOGGER = logging.getLogger(__name__)


class DiscordAdapterApplication:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._session = aiohttp.ClientSession()
        self._store = AdapterStore(settings.database_path)
        self._client = DiscordBridgeClient(
            self._session,
            settings.core_url,
            settings.internal_token,
            self._store,
        )
        self._discord_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._client.start_spool()
        self._discord_task = asyncio.create_task(
            self._client.start(self._settings.discord_token),
            name="discord-client",
        )

    async def wait(self) -> None:
        spool_task = self._client.spool_task
        if self._discord_task is None or spool_task is None:
            raise RuntimeError("Discord adapter background tasks are not started")
        done, _ = await asyncio.wait(
            (self._discord_task, spool_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in done:
            task.result()
        raise RuntimeError("A Discord adapter background task stopped unexpectedly")

    async def close(self) -> None:
        await self._client.close()
        if self._discord_task is not None:
            self._discord_task.cancel()
            await asyncio.gather(self._discord_task, return_exceptions=True)
        await self._session.close()
        self._store.close()

    def web_application(self) -> web.Application:
        app = web.Application(client_max_size=self._settings.max_image_bytes + 1024 * 1024)
        app.router.add_get("/health", self.health)
        app.router.add_post("/v1/messages", self.send_message)
        app.router.add_patch("/v1/messages/{message_id}", self.edit_message)
        return app

    async def health(self, request: web.Request) -> web.Response:
        del request
        user = self._client.user
        connected = user is not None and self._client.is_ready() and not self._client.is_closed()
        store_ok = self._store.health_probe()
        ok = connected and store_ok and self._client.spool_alive
        return web.json_response(
            {
                "ok": ok,
                "connected": connected,
                "spool_alive": self._client.spool_alive,
                "pending_events": self._client.pending_count,
                "store_ok": store_ok,
                "user_id": str(user.id) if user is not None else None,
            },
            status=200 if ok else 503,
        )

    async def send_message(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response(
                {"ok": False, "error": "invalid internal token"},
                status=401,
            )
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
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        except Exception as exc:
            _LOGGER.exception("Discord outbound delivery failed")
            return web.json_response({"ok": False, "error": str(exc)}, status=502)
        return web.json_response({"ok": True, "message_id": message_id})

    async def edit_message(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response(
                {"ok": False, "error": "invalid internal token"},
                status=401,
            )
        try:
            raw_object: object = await request.json()
            if not isinstance(raw_object, dict):
                raise ValueError("request body must be an object")
            payload = cast(dict[str, object], raw_object)
            message_id = self._required_string(
                {"message_id": request.match_info.get("message_id")},
                "message_id",
            )
            edited_message_id = await self._client.edit_message(
                conversation_id=self._required_string(payload, "conversation_id"),
                message_id=message_id,
                text=self._optional_string(payload, "text"),
            )
        except (ValueError, json.JSONDecodeError) as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        except Exception as exc:
            _LOGGER.exception("Discord message edit failed")
            return web.json_response({"ok": False, "error": str(exc)}, status=502)
        return web.json_response({"ok": True, "message_id": edited_message_id})

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
        await adapter.wait()
    finally:
        await runner.cleanup()
        await adapter.close()


def run() -> None:
    asyncio.run(_run())
