from __future__ import annotations

import asyncio
import hmac
import logging
from contextlib import suppress
from typing import cast

import aiohttp
from aiohttp import web

from unified_inbox_core.adapter import AdapterClient
from unified_inbox_core.config import Settings
from unified_inbox_core.db import Database
from unified_inbox_core.models import InboundEvent
from unified_inbox_core.router import Router
from unified_inbox_core.telegram import TelegramClient

_LOGGER = logging.getLogger(__name__)


class CoreApplication:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._db = Database(settings.database_path)
        self._session = aiohttp.ClientSession()
        self._telegram = TelegramClient(
            self._session,
            settings.telegram_token,
            settings.max_image_bytes,
        )
        self._outbox_telegram = TelegramClient(
            self._session,
            settings.telegram_outbox_token,
            settings.max_image_bytes,
        )
        self._adapters = AdapterClient(
            self._session,
            settings.internal_token,
            {
                "discord": settings.discord_adapter_url,
                "steam": settings.steam_adapter_url,
            },
        )
        self._router = Router(
            self._db,
            self._telegram,
            self._adapters,
            self._session,
            settings.telegram_chat_id,
            settings.telegram_allowed_user_id,
            settings.max_image_bytes,
            self._outbox_telegram,
        )
        self._poll_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        me = await self._telegram.get_me()
        outbox_me = await self._outbox_telegram.get_me()
        _LOGGER.info(
            "Telegram bots connected as @%s and @%s",
            me.get("username"),
            outbox_me.get("username"),
        )
        await self._telegram.initialize_polling()
        self._poll_task = asyncio.create_task(self._poll_telegram(), name="telegram-poll")

    async def close(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._poll_task
        await self._session.close()
        self._db.close()

    def web_application(self) -> web.Application:
        app = web.Application(client_max_size=self._settings.max_image_bytes + 1024 * 1024)
        app.router.add_get("/health", self.health)
        app.router.add_post("/v1/events", self.inbound_event)
        return app

    async def health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def inbound_event(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            raise web.HTTPUnauthorized(text="invalid internal token")
        try:
            raw_object: object = await request.json()
        except (ValueError, aiohttp.ContentTypeError) as exc:
            raise web.HTTPBadRequest(text="request body must be JSON") from exc
        if not isinstance(raw_object, dict):
            raise web.HTTPBadRequest(text="request body must be an object")
        try:
            event = InboundEvent.from_mapping(cast(dict[str, object], raw_object))
            delivered = await self._router.handle_inbound(event)
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        except Exception:
            _LOGGER.exception("Inbound event delivery failed")
            raise web.HTTPBadGateway(text="event delivery failed") from None
        return web.json_response({"ok": True, "delivered": delivered})

    async def _poll_telegram(self) -> None:
        offset = self._db.get_state_int("telegram_offset", 0)
        while True:
            try:
                updates = await self._telegram.get_updates(
                    offset,
                    self._settings.telegram_poll_timeout,
                )
                for update in updates:
                    update_id = update.get("update_id")
                    if not isinstance(update_id, int):
                        continue
                    await self._router.handle_telegram_update(update)
                    offset = update_id + 1
                    self._db.set_state_int("telegram_offset", offset)
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception("Telegram polling iteration failed")
                await asyncio.sleep(3)

    def _authorized(self, request: web.Request) -> bool:
        authorization = request.headers.get("Authorization", "")
        prefix = "Bearer "
        if not authorization.startswith(prefix):
            return False
        return hmac.compare_digest(authorization[len(prefix) :], self._settings.internal_token)


async def _run() -> None:
    settings = Settings.from_environment()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    core = CoreApplication(settings)
    runner = web.AppRunner(core.web_application(), access_log=None)
    try:
        await core.start()
        await runner.setup()
        site = web.TCPSite(runner, settings.bind, settings.port)
        await site.start()
        _LOGGER.info("Core listening on %s:%s", settings.bind, settings.port)
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        await core.close()


def run() -> None:
    asyncio.run(_run())
