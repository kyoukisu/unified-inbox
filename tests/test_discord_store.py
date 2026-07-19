from pathlib import Path

import pytest

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


def test_discord_store_advances_message_watermark_with_enqueue(tmp_path: Path) -> None:
    store = AdapterStore(tmp_path / "discord.sqlite3")
    payload: dict[str, object] = {"event_id": "100", "text": "hello"}

    with pytest.raises(ValueError):
        store.enqueue_message_event(payload, channel_id=0, message_id=100)
    assert store.pending_count() == 0
    assert store.message_watermark(1) is None

    assert store.enqueue_message_event(payload, channel_id=1, message_id=100) is True
    assert store.message_watermark(1) == 100
    assert store.enqueue_message_event(payload, channel_id=1, message_id=100) is False
    assert store.message_watermark(1) == 100
    store.close()


def test_discord_store_persists_suppressed_bridge_watermark(tmp_path: Path) -> None:
    path = tmp_path / "discord.sqlite3"
    store = AdapterStore(path)
    store.record_suppressed_bridge_echo(channel_id=7, message_id=200)
    store.record_suppressed_bridge_echo(channel_id=7, message_id=199)
    store.close()

    reopened = AdapterStore(path)
    assert reopened.message_watermark(7) == 200
    assert reopened.pending_count() == 0
    reopened.close()


def test_discord_store_quarantines_one_event_and_unblocks_next(tmp_path: Path) -> None:
    path = tmp_path / "discord.sqlite3"
    store = AdapterStore(path)
    assert store.enqueue_event({"event_id": "bad"}) is True
    assert store.enqueue_event({"event_id": "good"}) is True
    bad = store.peek_event()
    assert bad is not None
    store.fail_event_attempt(bad.sequence, "first failure")

    store.quarantine_event(bad.sequence, "core returned HTTP 400")

    assert store.dead_letter_count() == 1
    pending = store.peek_event()
    assert pending is not None
    assert pending.event_id == "good"
    store.close()

    reopened = AdapterStore(path)
    assert reopened.dead_letter_count() == 1
    assert reopened.pending_count() == 1
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
