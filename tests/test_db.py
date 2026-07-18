import time
from pathlib import Path

from unified_inbox_core.db import Database


def test_database_persists_conversation_and_message_mapping(tmp_path: Path) -> None:
    path = tmp_path / "bridge.sqlite3"
    db = Database(path)
    conversation = db.create_conversation("steam", "76561198000000000", "Alice", 42)
    db.store_message_copy(conversation.id, "1700000000:0", 100, "inbound")
    db.store_presence("steam", "76561198000000000", "online")
    db.close()

    reopened = Database(path)
    loaded = reopened.get_conversation("steam", "76561198000000000")
    assert loaded == conversation
    assert reopened.get_conversation_by_topic(42) == conversation
    assert reopened.telegram_message_for_external(conversation.id, "1700000000:0") == 100
    assert reopened.external_message_for_telegram(conversation.id, 100) == "1700000000:0"
    assert reopened.get_presence("steam", "76561198000000000") == "online"
    reopened.close()


def test_failed_event_can_retry_but_completed_event_is_deduplicated(tmp_path: Path) -> None:
    db = Database(tmp_path / "bridge.sqlite3")

    assert db.claim_event("steam", "event-1") is True
    assert db.claim_event("steam", "event-1") is False

    db.fail_event("steam", "event-1", "temporary failure")
    assert db.claim_event("steam", "event-1") is True

    db.finish_event("steam", "event-1")
    assert db.claim_event("steam", "event-1") is False
    db.close()


def test_delivery_jobs_are_durable_deduplicated_and_recover_leases(tmp_path: Path) -> None:
    path = tmp_path / "bridge.sqlite3"
    db = Database(path)
    queued = db.enqueue_external_event("steam", "event-1", "steam:alice", "{}")
    duplicate = db.enqueue_external_event("steam", "event-1", "steam:alice", "{}")

    assert queued.created is True
    assert duplicate.created is False
    assert duplicate.job_id == queued.job_id

    now = time.time()
    leased = db.claim_next_job(lease_seconds=300, now=now)
    assert leased is not None
    assert leased.id == queued.job_id
    assert leased.attempt_count == 1
    db.close()

    reopened = Database(path)
    assert reopened.recover_leases(now=now + 1) == 1
    recovered = reopened.claim_next_job(lease_seconds=300, now=now + 1)
    assert recovered is not None
    assert recovered.id == queued.job_id
    assert recovered.attempt_count == 2
    reopened.complete_job(recovered)
    assert reopened.claim_next_job(lease_seconds=300, now=now + 2) is None
    reopened.close()


def test_failed_job_blocks_only_its_conversation_until_retry(tmp_path: Path) -> None:
    db = Database(tmp_path / "bridge.sqlite3")
    first = db.enqueue_external_event("steam", "first", "steam:alice", "{}")
    second = db.enqueue_external_event("steam", "second", "steam:alice", "{}")
    other = db.enqueue_external_event("discord", "other", "discord:bob", "{}")

    first_job = db.claim_next_job(300)
    assert first_job is not None and first_job.id == first.job_id
    db.fail_job(first_job, "permanent")

    other_job = db.claim_next_job(300)
    assert other_job is not None and other_job.id == other.job_id
    db.complete_job(other_job)
    assert db.claim_next_job(300) is None

    assert db.retry_failed_jobs("steam:alice") == [first.job_id]
    retried = db.claim_next_job(300)
    assert retried is not None and retried.id == first.job_id
    db.complete_job(retried)
    unblocked = db.claim_next_job(300)
    assert unblocked is not None and unblocked.id == second.job_id
    db.close()


def test_legacy_failures_remain_visible_after_migration(tmp_path: Path) -> None:
    path = tmp_path / "bridge.sqlite3"
    db = Database(path)
    assert db.claim_event("discord", "legacy-message") is True
    db.fail_event("discord", "legacy-message", "old delivery failed")
    db.close()

    migrated = Database(path)
    assert migrated.legacy_failure_count() == 1
    failures = migrated.list_legacy_failures()
    assert [(item.source, item.event_id, item.error) for item in failures] == [
        ("discord", "legacy-message", "old delivery failed")
    ]
    assert migrated.job_counts()["legacy_unrecoverable_events"] == 1
    migrated.close()


def test_telegram_update_and_offset_are_committed_together(tmp_path: Path) -> None:
    db = Database(tmp_path / "bridge.sqlite3")
    result = db.enqueue_telegram_update(
        "400",
        "discord:dm-1",
        '{"update_id":400}',
        telegram_message_id=88,
        next_offset=401,
    )

    assert result.created is True
    assert db.get_state_int("telegram_offset", 0) == 401
    job = db.claim_next_job(300)
    assert job is not None
    assert job.source == "telegram"
    assert job.telegram_message_id == 88
    db.close()
