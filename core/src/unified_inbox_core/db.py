from __future__ import annotations

import sqlite3
from pathlib import Path

from unified_inbox_core.models import Conversation, Platform

_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY,
    platform TEXT NOT NULL CHECK (platform IN ('discord', 'steam')),
    external_chat_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    telegram_topic_id INTEGER NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (platform, external_chat_id)
);

CREATE TABLE IF NOT EXISTS message_copies (
    id INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    external_message_id TEXT NOT NULL,
    telegram_message_id INTEGER NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('inbound', 'outbound')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (conversation_id, external_message_id),
    UNIQUE (conversation_id, telegram_message_id)
);

CREATE TABLE IF NOT EXISTS processed_events (
    source TEXT NOT NULL,
    event_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('processing', 'done', 'failed')),
    error TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (source, event_id)
);

CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path)
        self._connection.row_factory = sqlite3.Row
        self._connection.executescript(_SCHEMA)

    def close(self) -> None:
        self._connection.close()

    def claim_event(self, source: str, event_id: str) -> bool:
        with self._connection:
            row = self._connection.execute(
                "SELECT status FROM processed_events WHERE source = ? AND event_id = ?",
                (source, event_id),
            ).fetchone()
            if row is not None and row["status"] in ("processing", "done"):
                return False
            self._connection.execute(
                """
                INSERT INTO processed_events (source, event_id, status, error)
                VALUES (?, ?, 'processing', NULL)
                ON CONFLICT (source, event_id) DO UPDATE SET
                    status = 'processing', error = NULL, updated_at = CURRENT_TIMESTAMP
                """,
                (source, event_id),
            )
        return True

    def finish_event(self, source: str, event_id: str) -> None:
        with self._connection:
            self._connection.execute(
                """
                UPDATE processed_events
                SET status = 'done', error = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE source = ? AND event_id = ?
                """,
                (source, event_id),
            )

    def fail_event(self, source: str, event_id: str, error: str) -> None:
        with self._connection:
            self._connection.execute(
                """
                UPDATE processed_events
                SET status = 'failed', error = ?, updated_at = CURRENT_TIMESTAMP
                WHERE source = ? AND event_id = ?
                """,
                (error[:1000], source, event_id),
            )

    def get_conversation(self, platform: Platform, external_chat_id: str) -> Conversation | None:
        row = self._connection.execute(
            """
            SELECT id, platform, external_chat_id, display_name, telegram_topic_id
            FROM conversations
            WHERE platform = ? AND external_chat_id = ?
            """,
            (platform, external_chat_id),
        ).fetchone()
        return self._conversation_from_row(row) if row is not None else None

    def get_conversation_by_topic(self, topic_id: int) -> Conversation | None:
        row = self._connection.execute(
            """
            SELECT id, platform, external_chat_id, display_name, telegram_topic_id
            FROM conversations
            WHERE telegram_topic_id = ?
            """,
            (topic_id,),
        ).fetchone()
        return self._conversation_from_row(row) if row is not None else None

    def create_conversation(
        self,
        platform: Platform,
        external_chat_id: str,
        display_name: str,
        topic_id: int,
    ) -> Conversation:
        with self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO conversations (
                    platform, external_chat_id, display_name, telegram_topic_id
                ) VALUES (?, ?, ?, ?)
                """,
                (platform, external_chat_id, display_name, topic_id),
            )
        row_id = cursor.lastrowid
        if row_id is None:
            raise RuntimeError("SQLite did not return a conversation row ID")
        return Conversation(
            id=row_id,
            platform=platform,
            external_chat_id=external_chat_id,
            display_name=display_name,
            telegram_topic_id=topic_id,
        )

    def update_display_name(self, conversation_id: int, display_name: str) -> None:
        with self._connection:
            self._connection.execute(
                """
                UPDATE conversations
                SET display_name = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (display_name, conversation_id),
            )

    def store_message_copy(
        self,
        conversation_id: int,
        external_message_id: str,
        telegram_message_id: int,
        direction: str,
    ) -> None:
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO message_copies (
                    conversation_id, external_message_id, telegram_message_id, direction
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT (conversation_id, external_message_id) DO UPDATE SET
                    telegram_message_id = excluded.telegram_message_id,
                    direction = excluded.direction
                """,
                (conversation_id, external_message_id, telegram_message_id, direction),
            )

    def telegram_message_for_external(
        self,
        conversation_id: int,
        external_message_id: str,
    ) -> int | None:
        row = self._connection.execute(
            """
            SELECT telegram_message_id
            FROM message_copies
            WHERE conversation_id = ? AND external_message_id = ?
            """,
            (conversation_id, external_message_id),
        ).fetchone()
        return int(row["telegram_message_id"]) if row is not None else None

    def external_message_for_telegram(
        self,
        conversation_id: int,
        telegram_message_id: int,
    ) -> str | None:
        row = self._connection.execute(
            """
            SELECT external_message_id
            FROM message_copies
            WHERE conversation_id = ? AND telegram_message_id = ?
            """,
            (conversation_id, telegram_message_id),
        ).fetchone()
        return str(row["external_message_id"]) if row is not None else None

    def get_state_int(self, key: str, default: int) -> int:
        row = self._connection.execute(
            "SELECT value FROM state WHERE key = ?",
            (key,),
        ).fetchone()
        return int(row["value"]) if row is not None else default

    def set_state_int(self, key: str, value: int) -> None:
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO state (key, value) VALUES (?, ?)
                ON CONFLICT (key) DO UPDATE SET value = excluded.value
                """,
                (key, str(value)),
            )

    @staticmethod
    def _conversation_from_row(row: sqlite3.Row) -> Conversation:
        platform = str(row["platform"])
        if platform not in ("discord", "steam"):
            raise RuntimeError(f"Invalid platform in database: {platform}")
        return Conversation(
            id=int(row["id"]),
            platform=platform,
            external_chat_id=str(row["external_chat_id"]),
            display_name=str(row["display_name"]),
            telegram_topic_id=int(row["telegram_topic_id"]),
        )
