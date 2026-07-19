from __future__ import annotations

import asyncio
import hmac
import logging
import time
from typing import cast

import aiohttp
from aiohttp import web

from unified_inbox_core.adapter import AdapterClient
from unified_inbox_core.config import Settings
from unified_inbox_core.db import Database
from unified_inbox_core.delivery import DeliveryWorker
from unified_inbox_core.models import external_event_from_mapping
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
        self._worker = DeliveryWorker(
            self._db,
            self._router,
            max_attempts=settings.delivery_max_attempts,
            lease_seconds=settings.delivery_lease_seconds,
            max_retry_seconds=settings.delivery_retry_max_seconds,
        )
        self._poll_task: asyncio.Task[None] | None = None
        self._started_at = time.monotonic()
        self._last_poll_success = self._started_at

    async def start(self) -> None:
        me = await self._telegram.get_me()
        outbox_me = await self._outbox_telegram.get_me()
        _LOGGER.info(
            "Telegram bots connected as @%s and @%s",
            me.get("username"),
            outbox_me.get("username"),
        )
        await self._telegram.initialize_polling()
        self._worker.start()
        self._poll_task = asyncio.create_task(self._poll_telegram(), name="telegram-poll")

    async def wait(self) -> None:
        if self._poll_task is None or self._worker.task is None:
            raise RuntimeError("Core background tasks are not started")
        done, _ = await asyncio.wait(
            (self._poll_task, self._worker.task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in done:
            task.result()
        raise RuntimeError("A core background task stopped unexpectedly")

    async def close(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
            await asyncio.gather(self._poll_task, return_exceptions=True)
        await self._worker.close()
        await self._session.close()
        self._db.close()

    def web_application(self) -> web.Application:
        app = web.Application(client_max_size=self._settings.max_image_bytes + 1024 * 1024)
        app.router.add_get("/health", self.health)
        app.router.add_get("/v1/conversations/{platform}", self.conversations)
        app.router.add_post("/v1/events", self.inbound_event)
        return app

    async def health(self, request: web.Request) -> web.Response:
        del request
        try:
            database_ok = self._db.health_probe()
        except Exception:
            _LOGGER.exception("Core database health probe failed")
            database_ok = False
        poll_alive = self._poll_task is not None and not self._poll_task.done()
        worker_task = self._worker.task
        worker_alive = worker_task is not None and not worker_task.done()
        poll_age = max(0.0, time.monotonic() - self._last_poll_success)
        stale_after = self._settings.telegram_poll_timeout * 2 + 15
        poll_fresh = poll_age <= stale_after
        jobs = self._db.job_counts() if database_ok else {}
        failed = jobs.get("failed", 0) if jobs else 0
        legacy = jobs.get("legacy_unrecoverable_events", 0) if jobs else 0
        ok = database_ok and poll_alive and worker_alive and poll_fresh
        payload: dict[str, object] = {
            "ok": ok,
            "degraded": bool(failed or legacy),
            "database": {"writable": database_ok},
            "telegram_poll": {
                "alive": poll_alive,
                "last_success_age": round(poll_age, 1),
            },
            "worker": {
                "alive": worker_alive,
                "last_heartbeat_age": round(self._worker.last_heartbeat_age, 1),
            },
            "jobs": jobs,
        }
        return web.json_response(payload, status=200 if ok else 503)

    async def conversations(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"ok": False, "error": "invalid internal token"}, status=401)
        platform = request.match_info.get("platform")
        if platform not in ("discord", "steam"):
            return web.json_response({"ok": False, "error": "unknown platform"}, status=400)
        conversations = self._db.list_conversations(platform)
        return web.json_response(
            {
                "ok": True,
                "conversations": [
                    {
                        "conversation_id": conversation.external_chat_id,
                        "display_name": conversation.display_name,
                    }
                    for conversation in conversations
                ],
            }
        )

    async def inbound_event(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"ok": False, "error": "invalid internal token"}, status=401)
        try:
            raw_object: object = await request.json()
        except (ValueError, aiohttp.ContentTypeError):
            return web.json_response(
                {"ok": False, "error": "request body must be JSON"},
                status=400,
            )
        if not isinstance(raw_object, dict):
            return web.json_response(
                {"ok": False, "error": "request body must be an object"},
                status=400,
            )
        try:
            event = external_event_from_mapping(cast(dict[str, object], raw_object))
            result = self._router.enqueue_inbound(event)
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        except Exception:
            _LOGGER.exception("Inbound event enqueue failed")
            return web.json_response(
                {"ok": False, "error": "event enqueue failed"},
                status=503,
            )
        self._worker.wake()
        return web.json_response(
            {
                "ok": True,
                "accepted": result.created,
                "delivered": result.state in ("succeeded", "legacy_succeeded"),
                "job_id": result.job_id,
                "state": result.state,
            },
            status=202 if result.created else 200,
        )

    async def _poll_telegram(self) -> None:
        offset = self._db.get_state_int("telegram_offset", 0)
        while True:
            try:
                updates = await self._telegram.get_updates(
                    offset,
                    self._settings.telegram_poll_timeout,
                )
                self._last_poll_success = time.monotonic()
                for update in updates:
                    update_id = update.get("update_id")
                    if not isinstance(update_id, int):
                        continue
                    result = self._router.enqueue_telegram_update(update)
                    offset = update_id + 1
                    if result.created:
                        await self._router.set_pending_reaction(update)
                    self._worker.wake()
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
        await core.wait()
    finally:
        await runner.cleanup()
        await core.close()


def run() -> None:
    asyncio.run(_run())
