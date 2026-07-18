from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from unified_inbox_core.db import Database
from unified_inbox_core.errors import DeliveryError

if TYPE_CHECKING:
    from unified_inbox_core.models import DeliveryJob
    from unified_inbox_core.router import Router

_LOGGER = logging.getLogger(__name__)


class DeliveryWorker:
    def __init__(
        self,
        db: Database,
        router: Router,
        *,
        max_attempts: int = 10,
        lease_seconds: float = 300,
        max_retry_seconds: float = 300,
    ) -> None:
        self._db = db
        self._router = router
        self._max_attempts = max_attempts
        self._lease_seconds = lease_seconds
        self._max_retry_seconds = max_retry_seconds
        self._wake_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._last_heartbeat = time.monotonic()

    @property
    def task(self) -> asyncio.Task[None] | None:
        return self._task

    @property
    def last_heartbeat_age(self) -> float:
        return max(0.0, time.monotonic() - self._last_heartbeat)

    def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("Delivery worker is already started")
        self._db.recover_leases()
        self._task = asyncio.create_task(self._run(), name="delivery-worker")

    async def close(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    def wake(self) -> None:
        self._wake_event.set()

    async def run_once(self) -> bool:
        self._last_heartbeat = time.monotonic()
        job = self._db.claim_next_job(self._lease_seconds)
        if job is None:
            return False

        try:
            await self._router.process_job(job)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._handle_failure(job, exc)
        else:
            self._db.complete_job(job)
            await self._router.set_delivery_reaction(job, "👍")
        finally:
            self._last_heartbeat = time.monotonic()
        return True

    async def _run(self) -> None:
        while True:
            processed = await self.run_once()
            if processed:
                continue
            self._wake_event.clear()
            try:
                await asyncio.wait_for(self._wake_event.wait(), timeout=0.5)
            except TimeoutError:
                pass

    async def _handle_failure(self, job: DeliveryJob, exc: Exception) -> None:
        message = f"{type(exc).__name__}: {exc}"
        retryable = not isinstance(exc, DeliveryError) or exc.retryable
        retry_after = exc.retry_after if isinstance(exc, DeliveryError) else None
        if not retryable or job.attempt_count >= self._max_attempts:
            self._db.fail_job(job, message)
            _LOGGER.error(
                "Delivery job %s failed permanently after %s attempts: %s",
                job.id,
                job.attempt_count,
                message,
            )
            await self._router.set_delivery_reaction(job, "👎")
            return

        delay = (
            retry_after
            if retry_after is not None
            else min(2 ** max(0, job.attempt_count - 1), self._max_retry_seconds)
        )
        available_at = time.time() + max(0.0, delay)
        self._db.reschedule_job(job.id, message, available_at)
        _LOGGER.warning(
            "Delivery job %s will retry in %.1fs after attempt %s: %s",
            job.id,
            delay,
            job.attempt_count,
            message,
        )
