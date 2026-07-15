from pathlib import Path

from unified_inbox_core.db import Database


def test_database_persists_conversation_and_message_mapping(tmp_path: Path) -> None:
    path = tmp_path / "bridge.sqlite3"
    db = Database(path)
    conversation = db.create_conversation("steam", "76561198000000000", "Alice", 42)
    db.store_message_copy(conversation.id, "1700000000:0", 100, "inbound")
    db.close()

    reopened = Database(path)
    loaded = reopened.get_conversation("steam", "76561198000000000")
    assert loaded == conversation
    assert reopened.get_conversation_by_topic(42) == conversation
    assert reopened.telegram_message_for_external(conversation.id, "1700000000:0") == 100
    assert reopened.external_message_for_telegram(conversation.id, 100) == "1700000000:0"
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
