from pathlib import Path

from discord_adapter.store import AdapterStore


def test_discord_store_persists_pending_events(tmp_path: Path) -> None:
    path = tmp_path / "discord.sqlite3"
    first = AdapterStore(path)
    payload: dict[str, object] = {"event_id": "message-1", "text": "hello"}
    assert first.enqueue_event(payload) is True
    assert first.enqueue_event(payload) is False
    first.close()

    reopened = AdapterStore(path)
    pending = reopened.peek_event()
    assert pending is not None
    assert pending.event_id == "message-1"
    assert pending.payload == payload
    reopened.delete_event(pending.sequence)
    assert reopened.pending_count() == 0
    reopened.close()


def test_discord_store_persists_outbound_idempotency(tmp_path: Path) -> None:
    path = tmp_path / "discord.sqlite3"
    first = AdapterStore(path)
    record = first.begin_outbound("telegram:1", "dm-1", 123)
    assert record.state == "sending"
    first.complete_outbound("telegram:1", "discord-message-1")
    first.close()

    reopened = AdapterStore(path)
    completed = reopened.get_outbound("telegram:1")
    assert completed is not None
    assert completed.state == "completed"
    assert completed.message_id == "discord-message-1"
    assert reopened.is_bridge_nonce(123) is True
    reopened.close()
