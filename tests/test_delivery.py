from __future__ import annotations

import time
from pathlib import Path
from typing import cast

import pytest

from unified_inbox_core.db import Database
from unified_inbox_core.delivery import DeliveryWorker
from unified_inbox_core.errors import PermanentDeliveryError
from unified_inbox_core.models import DeliveryJob
from unified_inbox_core.router import Router
from unified_inbox_core.telegram import TelegramError


class FakeRouter:
    def __init__(self, outcomes: list[Exception | None]) -> None:
        self.outcomes = outcomes
        self.processed: list[int] = []
        self.reactions: list[tuple[int, str]] = []

    async def process_job(self, job: DeliveryJob) -> None:
        self.processed.append(job.id)
        outcome = self.outcomes.pop(0)
        if outcome is not None:
            raise outcome

    async def set_delivery_reaction(self, job: DeliveryJob, emoji: str) -> None:
        self.reactions.append((job.id, emoji))


@pytest.mark.asyncio
async def test_worker_completes_durable_job_and_marks_success(tmp_path: Path) -> None:
    db = Database(tmp_path / "bridge.sqlite3")
    queued = db.enqueue_external_event("steam", "event-1", "steam:alice", "{}")
    router = FakeRouter([None])
    worker = DeliveryWorker(db, cast(Router, router))

    assert await worker.run_once() is True

    assert router.processed == [queued.job_id]
    assert router.reactions == [(queued.job_id, "👍")]
    assert db.job_counts()["succeeded"] == 1
    db.close()


@pytest.mark.asyncio
async def test_worker_honors_telegram_retry_after(tmp_path: Path) -> None:
    db = Database(tmp_path / "bridge.sqlite3")
    queued = db.enqueue_external_event("steam", "event-1", "steam:alice", "{}")
    router = FakeRouter([TelegramError("rate limited", retry_after=17)])
    worker = DeliveryWorker(db, cast(Router, router))
    before = time.time()

    assert await worker.run_once() is True
    assert db.claim_next_job(300, now=before + 16) is None
    retried = db.claim_next_job(300, now=before + 18)
    assert retried is not None
    assert retried.id == queued.job_id
    db.close()


@pytest.mark.asyncio
async def test_worker_keeps_permanent_failure_visible(tmp_path: Path) -> None:
    db = Database(tmp_path / "bridge.sqlite3")
    queued = db.enqueue_external_event("discord", "event-1", "discord:bob", "{}")
    router = FakeRouter([PermanentDeliveryError("unsupported")])
    worker = DeliveryWorker(db, cast(Router, router))

    assert await worker.run_once() is True

    failures = db.list_failures("discord:bob")
    assert [failure.job_id for failure in failures] == [queued.job_id]
    assert router.reactions == [(queued.job_id, "👎")]
    db.close()
