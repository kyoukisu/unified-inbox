from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast


@dataclass(frozen=True, slots=True)
class PendingEvent:
    sequence: int
    event_id: str
    payload: dict[str, object]
    attempt_count: int


@dataclass(frozen=True, slots=True)
class OutboundRecord:
    idempotency_key: str
    conversation_id: str
    nonce: int
    state: str
    message_id: str | None
    created_at: float


class AdapterStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, timeout=5)
        self._connection.row_factory = sqlite3.Row
        self._connection.executescript(
            """
            PRAGMA journal_mode = WAL;
            PRAGMA synchronous = FULL;
            PRAGMA busy_timeout = 5000;

            CREATE TABLE IF NOT EXISTS pending_events (
                sequence INTEGER PRIMARY KEY,
                event_id TEXT NOT NULL UNIQUE,
                payload_json TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS outbound_deliveries (
                idempotency_key TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                nonce INTEGER NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('sending', 'completed')),
                message_id TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS dm_channel_watermarks (
                channel_id INTEGER PRIMARY KEY,
                message_id INTEGER NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS dead_letter_events (
                id INTEGER PRIMARY KEY,
                source_sequence INTEGER NOT NULL,
                event_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                attempt_count INTEGER NOT NULL,
                last_error TEXT NOT NULL,
                created_at REAL NOT NULL,
                quarantined_at REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS outbound_nonce
                ON outbound_deliveries(nonce);
            CREATE INDEX IF NOT EXISTS outbound_message
                ON outbound_deliveries(message_id);
            """
        )

    def close(self) -> None:
        self._connection.close()

    def enqueue_event(self, payload: dict[str, object]) -> bool:
        event_id = payload.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("pending Discord event has no event_id")
        now = time.time()
        with self._connection:
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO pending_events (
                    event_id, payload_json, attempt_count, last_error,
                    created_at, updated_at
                ) VALUES (?, ?, 0, NULL, ?, ?)
                """,
                (
                    event_id,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    now,
                    now,
                ),
            )
        return cursor.rowcount > 0

    def enqueue_message_event(
        self,
        payload: dict[str, object],
        channel_id: int,
        message_id: int,
    ) -> bool:
        event_id = self._event_id(payload)
        now = time.time()
        payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._connection:
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO pending_events (
                    event_id, payload_json, attempt_count, last_error,
                    created_at, updated_at
                ) VALUES (?, ?, 0, NULL, ?, ?)
                """,
                (event_id, payload_json, now, now),
            )
            self._advance_message_watermark(channel_id, message_id, now)
        return cursor.rowcount > 0

    def message_watermark(self, channel_id: int) -> int | None:
        row = self._connection.execute(
            "SELECT message_id FROM dm_channel_watermarks WHERE channel_id = ?",
            (channel_id,),
        ).fetchone()
        return int(row["message_id"]) if row is not None else None

    def record_suppressed_bridge_echo(self, channel_id: int, message_id: int) -> None:
        with self._connection:
            self._advance_message_watermark(channel_id, message_id, time.time())

    def peek_event(self) -> PendingEvent | None:
        row = self._connection.execute(
            """
            SELECT sequence, event_id, payload_json, attempt_count
            FROM pending_events
            ORDER BY sequence
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        raw: object = json.loads(str(row["payload_json"]))
        if not isinstance(raw, dict):
            raise RuntimeError("stored Discord event payload is not an object")
        return PendingEvent(
            sequence=int(row["sequence"]),
            event_id=str(row["event_id"]),
            payload=cast(dict[str, object], raw),
            attempt_count=int(row["attempt_count"]),
        )

    def fail_event_attempt(self, sequence: int, error: str) -> None:
        with self._connection:
            self._connection.execute(
                """
                UPDATE pending_events
                SET attempt_count = attempt_count + 1,
                    last_error = ?, updated_at = ?
                WHERE sequence = ?
                """,
                (error, time.time(), sequence),
            )

    def delete_event(self, sequence: int) -> None:
        with self._connection:
            self._connection.execute(
                "DELETE FROM pending_events WHERE sequence = ?",
                (sequence,),
            )

    def pending_count(self) -> int:
        row = self._connection.execute("SELECT COUNT(*) AS count FROM pending_events").fetchone()
        return int(row["count"]) if row is not None else 0

    def quarantine_event(self, sequence: int, error: str) -> None:
        now = time.time()
        with self._connection:
            inserted = self._connection.execute(
                """
                INSERT INTO dead_letter_events (
                    source_sequence, event_id, payload_json, attempt_count,
                    last_error, created_at, quarantined_at
                )
                SELECT sequence, event_id, payload_json, attempt_count,
                       ?, created_at, ?
                FROM pending_events
                WHERE sequence = ?
                """,
                (error, now, sequence),
            )
            if inserted.rowcount != 1:
                raise RuntimeError(f"pending Discord event {sequence} disappeared")
            deleted = self._connection.execute(
                "DELETE FROM pending_events WHERE sequence = ?",
                (sequence,),
            )
            if deleted.rowcount != 1:
                raise RuntimeError(f"pending Discord event {sequence} was not quarantined")

    def dead_letter_count(self) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) AS count FROM dead_letter_events"
        ).fetchone()
        return int(row["count"]) if row is not None else 0

    def begin_outbound(
        self,
        idempotency_key: str,
        conversation_id: str,
        nonce: int,
    ) -> OutboundRecord:
        now = time.time()
        with self._connection:
            self._connection.execute(
                """
                INSERT OR IGNORE INTO outbound_deliveries (
                    idempotency_key, conversation_id, nonce, state,
                    message_id, created_at, updated_at
                ) VALUES (?, ?, ?, 'sending', NULL, ?, ?)
                """,
                (idempotency_key, conversation_id, nonce, now, now),
            )
        record = self.get_outbound(idempotency_key)
        if record is None:
            raise RuntimeError("Discord outbound delivery record disappeared")
        return record

    def get_outbound(self, idempotency_key: str) -> OutboundRecord | None:
        row = self._connection.execute(
            """
            SELECT idempotency_key, conversation_id, nonce, state, message_id, created_at
            FROM outbound_deliveries
            WHERE idempotency_key = ?
            """,
            (idempotency_key,),
        ).fetchone()
        if row is None:
            return None
        return OutboundRecord(
            idempotency_key=str(row["idempotency_key"]),
            conversation_id=str(row["conversation_id"]),
            nonce=int(row["nonce"]),
            state=str(row["state"]),
            message_id=str(row["message_id"]) if row["message_id"] is not None else None,
            created_at=float(row["created_at"]),
        )

    def complete_outbound(self, idempotency_key: str, message_id: str) -> None:
        now = time.time()
        with self._connection:
            self._connection.execute(
                """
                UPDATE outbound_deliveries
                SET state = 'completed', message_id = ?, updated_at = ?
                WHERE idempotency_key = ?
                """,
                (message_id, now, idempotency_key),
            )
            self._connection.execute(
                """
                DELETE FROM outbound_deliveries
                WHERE state = 'completed' AND updated_at < ?
                """,
                (now - 16 * 24 * 60 * 60,),
            )

    def is_bridge_nonce(self, nonce: int) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM outbound_deliveries WHERE nonce = ? LIMIT 1",
            (nonce,),
        ).fetchone()
        return row is not None

    def is_bridge_message(self, message_id: int) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM outbound_deliveries WHERE message_id = ? LIMIT 1",
            (str(message_id),),
        ).fetchone()
        return row is not None

    @staticmethod
    def _event_id(payload: dict[str, object]) -> str:
        event_id = payload.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("pending Discord event has no event_id")
        return event_id

    def _advance_message_watermark(self, channel_id: int, message_id: int, now: float) -> None:
        if channel_id <= 0 or message_id <= 0:
            raise ValueError("Discord channel and message IDs must be positive")
        self._connection.execute(
            """
            INSERT INTO dm_channel_watermarks (channel_id, message_id, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET
                message_id = excluded.message_id,
                updated_at = excluded.updated_at
            WHERE excluded.message_id > dm_channel_watermarks.message_id
            """,
            (channel_id, message_id, now),
        )

    def health_probe(self) -> bool:
        row = self._connection.execute("PRAGMA quick_check").fetchone()
        if row is None or str(row[0]).lower() != "ok":
            return False
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._connection.execute("UPDATE pending_events SET updated_at = updated_at WHERE 0")
            self._connection.rollback()
        except Exception:
            self._connection.rollback()
            raise
        return True
