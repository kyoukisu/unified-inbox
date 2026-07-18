from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from unified_inbox_core.models import (
    Conversation,
    DeliveryJob,
    EnqueueResult,
    FailureSummary,
    IngressKind,
    JobKind,
    JobSource,
    JobState,
    LegacyFailure,
    Platform,
)

_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;

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

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS ingress_events (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL CHECK (source IN ('discord', 'steam', 'telegram')),
    event_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('external_event', 'telegram_update')),
    conversation_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    telegram_message_id INTEGER,
    received_at REAL NOT NULL,
    UNIQUE (source, event_id)
);

CREATE TABLE IF NOT EXISTS delivery_jobs (
    id INTEGER PRIMARY KEY,
    ingress_event_id INTEGER NOT NULL UNIQUE
        REFERENCES ingress_events(id) ON DELETE RESTRICT,
    conversation_key TEXT NOT NULL,
    kind TEXT NOT NULL
        CHECK (kind IN ('route_external_event', 'route_telegram_update')),
    state TEXT NOT NULL
        CHECK (state IN ('pending', 'leased', 'succeeded', 'failed')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    available_at REAL NOT NULL,
    lease_expires_at REAL,
    last_error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    CHECK (
        (state = 'leased' AND lease_expires_at IS NOT NULL)
        OR (state <> 'leased' AND lease_expires_at IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS delivery_jobs_runnable
    ON delivery_jobs(state, available_at, id);
CREATE INDEX IF NOT EXISTS delivery_jobs_conversation_order
    ON delivery_jobs(conversation_key, id, state);
CREATE INDEX IF NOT EXISTS delivery_jobs_failures
    ON delivery_jobs(state, conversation_key, id);

CREATE TABLE IF NOT EXISTS delivery_parts (
    job_id INTEGER NOT NULL REFERENCES delivery_jobs(id) ON DELETE CASCADE,
    part_key TEXT NOT NULL,
    destination_message_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (job_id, part_key)
);
"""


class Database:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, timeout=5)
        self._connection.row_factory = sqlite3.Row
        self._connection.executescript(_SCHEMA)
        now = time.time()
        with self._connection:
            self._connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (1, ?)",
                (now,),
            )
            self._connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (2, ?)",
                (now,),
            )
            self._connection.execute("PRAGMA user_version = 2")

    def close(self) -> None:
        self._connection.close()

    def enqueue_external_event(
        self,
        source: Platform,
        event_id: str,
        conversation_key: str,
        payload_json: str,
    ) -> EnqueueResult:
        return self._enqueue(
            source=source,
            event_id=event_id,
            ingress_kind="external_event",
            job_kind="route_external_event",
            conversation_key=conversation_key,
            payload_json=payload_json,
            telegram_message_id=None,
            telegram_offset=None,
        )

    def enqueue_telegram_update(
        self,
        event_id: str,
        conversation_key: str,
        payload_json: str,
        telegram_message_id: int | None,
        next_offset: int,
    ) -> EnqueueResult:
        return self._enqueue(
            source="telegram",
            event_id=event_id,
            ingress_kind="telegram_update",
            job_kind="route_telegram_update",
            conversation_key=conversation_key,
            payload_json=payload_json,
            telegram_message_id=telegram_message_id,
            telegram_offset=next_offset,
        )

    def _enqueue(
        self,
        *,
        source: JobSource,
        event_id: str,
        ingress_kind: IngressKind,
        job_kind: JobKind,
        conversation_key: str,
        payload_json: str,
        telegram_message_id: int | None,
        telegram_offset: int | None,
    ) -> EnqueueResult:
        now = time.time()
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self._connection.execute(
                """
                SELECT j.id, j.state
                FROM ingress_events AS e
                JOIN delivery_jobs AS j ON j.ingress_event_id = e.id
                WHERE e.source = ? AND e.event_id = ?
                """,
                (source, event_id),
            ).fetchone()
            if existing is not None:
                result = EnqueueResult(
                    job_id=int(existing["id"]),
                    created=False,
                    state=self._job_state(existing["state"]),
                )
            else:
                legacy = self._connection.execute(
                    "SELECT status FROM processed_events WHERE source = ? AND event_id = ?",
                    (source, event_id),
                ).fetchone()
                if legacy is not None and legacy["status"] == "done":
                    result = EnqueueResult(
                        job_id=None,
                        created=False,
                        state="legacy_succeeded",
                    )
                else:
                    event_cursor = self._connection.execute(
                        """
                        INSERT INTO ingress_events (
                            source, event_id, kind, conversation_key, payload_json,
                            telegram_message_id, received_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            source,
                            event_id,
                            ingress_kind,
                            conversation_key,
                            payload_json,
                            telegram_message_id,
                            now,
                        ),
                    )
                    ingress_id = event_cursor.lastrowid
                    if ingress_id is None:
                        raise RuntimeError("SQLite did not return an ingress event row ID")
                    job_cursor = self._connection.execute(
                        """
                        INSERT INTO delivery_jobs (
                            ingress_event_id, conversation_key, kind, state,
                            attempt_count, available_at, lease_expires_at,
                            last_error, created_at, updated_at
                        ) VALUES (?, ?, ?, 'pending', 0, ?, NULL, NULL, ?, ?)
                        """,
                        (ingress_id, conversation_key, job_kind, now, now, now),
                    )
                    job_id = job_cursor.lastrowid
                    if job_id is None:
                        raise RuntimeError("SQLite did not return a delivery job row ID")
                    self._connection.execute(
                        """
                        INSERT INTO processed_events (source, event_id, status, error)
                        VALUES (?, ?, 'processing', NULL)
                        ON CONFLICT (source, event_id) DO UPDATE SET
                            status = 'processing', error = NULL, updated_at = CURRENT_TIMESTAMP
                        """,
                        (source, event_id),
                    )
                    result = EnqueueResult(job_id=job_id, created=True, state="pending")

            if telegram_offset is not None:
                self._connection.execute(
                    """
                    INSERT INTO state (key, value) VALUES ('telegram_offset', ?)
                    ON CONFLICT (key) DO UPDATE SET value = excluded.value
                    """,
                    (str(telegram_offset),),
                )
            self._connection.commit()
            return result
        except Exception:
            self._connection.rollback()
            raise

    def claim_next_job(self, lease_seconds: float, now: float | None = None) -> DeliveryJob | None:
        current = time.time() if now is None else now
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._connection.execute(
                """
                UPDATE delivery_jobs
                SET state = 'pending', lease_expires_at = NULL,
                    available_at = ?, updated_at = ?
                WHERE state = 'leased' AND lease_expires_at <= ?
                """,
                (current, current, current),
            )
            row = self._connection.execute(
                """
                SELECT j.id
                FROM delivery_jobs AS j
                WHERE j.state = 'pending'
                  AND j.available_at <= ?
                  AND NOT EXISTS (
                      SELECT 1
                      FROM delivery_jobs AS earlier
                      WHERE earlier.conversation_key = j.conversation_key
                        AND earlier.id < j.id
                        AND earlier.state IN ('pending', 'leased', 'failed')
                  )
                ORDER BY j.id
                LIMIT 1
                """,
                (current,),
            ).fetchone()
            if row is None:
                self._connection.commit()
                return None
            job_id = int(row["id"])
            self._connection.execute(
                """
                UPDATE delivery_jobs
                SET state = 'leased', attempt_count = attempt_count + 1,
                    lease_expires_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (current + lease_seconds, current, job_id),
            )
            claimed = self._select_job(job_id)
            self._connection.commit()
            if claimed is None:
                raise RuntimeError("Claimed delivery job disappeared")
            return claimed
        except Exception:
            self._connection.rollback()
            raise

    def renew_job_lease(
        self,
        job_id: int,
        lease_seconds: float,
        now: float | None = None,
    ) -> None:
        current = time.time() if now is None else now
        with self._connection:
            self._connection.execute(
                """
                UPDATE delivery_jobs
                SET lease_expires_at = ?, updated_at = ?
                WHERE id = ? AND state = 'leased'
                """,
                (current + lease_seconds, current, job_id),
            )

    def recover_leases(self, now: float | None = None) -> int:
        current = time.time() if now is None else now
        with self._connection:
            cursor = self._connection.execute(
                """
                UPDATE delivery_jobs
                SET state = 'pending', lease_expires_at = NULL,
                    available_at = ?, updated_at = ?
                WHERE state = 'leased'
                """,
                (current, current),
            )
        return cursor.rowcount

    def reschedule_job(self, job_id: int, error: str, available_at: float) -> None:
        with self._connection:
            self._connection.execute(
                """
                UPDATE delivery_jobs
                SET state = 'pending', available_at = ?, lease_expires_at = NULL,
                    last_error = ?, updated_at = ?
                WHERE id = ? AND state = 'leased'
                """,
                (available_at, error, time.time(), job_id),
            )

    def fail_job(self, job: DeliveryJob, error: str) -> None:
        now = time.time()
        with self._connection:
            self._connection.execute(
                """
                UPDATE delivery_jobs
                SET state = 'failed', lease_expires_at = NULL,
                    last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (error, now, job.id),
            )
            self._set_processed_event(job.source, job.event_id, "failed", error)

    def complete_job(self, job: DeliveryJob) -> None:
        now = time.time()
        with self._connection:
            self._connection.execute(
                """
                UPDATE delivery_jobs
                SET state = 'succeeded', lease_expires_at = NULL,
                    last_error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (now, job.id),
            )
            self._set_processed_event(job.source, job.event_id, "done", None)

    def get_delivery_part(self, job_id: int, part_key: str) -> str | None:
        row = self._connection.execute(
            """
            SELECT destination_message_id
            FROM delivery_parts
            WHERE job_id = ? AND part_key = ?
            """,
            (job_id, part_key),
        ).fetchone()
        return str(row["destination_message_id"]) if row is not None else None

    def store_delivery_part(self, job_id: int, part_key: str, message_id: str) -> None:
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO delivery_parts (
                    job_id, part_key, destination_message_id, created_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT (job_id, part_key) DO UPDATE SET
                    destination_message_id = excluded.destination_message_id
                """,
                (job_id, part_key, message_id, time.time()),
            )

    def retry_failed_jobs(
        self,
        conversation_key: str,
        job_id: int | None = None,
        retry_all: bool = False,
    ) -> list[int]:
        params: list[object] = [conversation_key]
        where = "state = 'failed' AND conversation_key = ?"
        if job_id is not None:
            where += " AND id = ?"
            params.append(job_id)
        limit = "" if retry_all or job_id is not None else " LIMIT 1"
        rows = self._connection.execute(
            f"SELECT id FROM delivery_jobs WHERE {where} ORDER BY id{limit}",
            params,
        ).fetchall()
        ids = [int(row["id"]) for row in rows]
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        now = time.time()
        with self._connection:
            self._connection.execute(
                f"""
                UPDATE delivery_jobs
                SET state = 'pending', attempt_count = 0, available_at = ?,
                    lease_expires_at = NULL, updated_at = ?
                WHERE id IN ({placeholders})
                """,
                (now, now, *ids),
            )
            for retry_id in ids:
                row = self._connection.execute(
                    """
                    SELECT e.source, e.event_id
                    FROM delivery_jobs AS j
                    JOIN ingress_events AS e ON e.id = j.ingress_event_id
                    WHERE j.id = ?
                    """,
                    (retry_id,),
                ).fetchone()
                if row is not None:
                    self._set_processed_event(
                        self._job_source(row["source"]),
                        str(row["event_id"]),
                        "processing",
                        None,
                    )
        return ids

    def list_failures(
        self,
        conversation_key: str | None = None,
        limit: int = 10,
    ) -> list[FailureSummary]:
        params: list[object] = []
        where = "j.state = 'failed'"
        if conversation_key is not None:
            where += " AND j.conversation_key = ?"
            params.append(conversation_key)
        params.append(limit)
        rows = self._connection.execute(
            f"""
            SELECT j.id, e.source, e.event_id, j.conversation_key,
                   j.attempt_count, j.last_error, j.created_at, j.updated_at
            FROM delivery_jobs AS j
            JOIN ingress_events AS e ON e.id = j.ingress_event_id
            WHERE {where}
            ORDER BY j.id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [
            FailureSummary(
                job_id=int(row["id"]),
                source=self._job_source(row["source"]),
                event_id=str(row["event_id"]),
                conversation_key=str(row["conversation_key"]),
                attempt_count=int(row["attempt_count"]),
                error=str(row["last_error"] or "unknown error"),
                created_at=float(row["created_at"]),
                updated_at=float(row["updated_at"]),
            )
            for row in rows
        ]

    def job_counts(self) -> dict[str, int | float | None]:
        counts: dict[str, int | float | None] = {
            "pending": 0,
            "leased": 0,
            "succeeded": 0,
            "failed": 0,
        }
        for row in self._connection.execute(
            "SELECT state, COUNT(*) AS count FROM delivery_jobs GROUP BY state"
        ):
            counts[str(row["state"])] = int(row["count"])
        oldest = self._connection.execute(
            """
            SELECT MIN(created_at) AS oldest
            FROM delivery_jobs
            WHERE state IN ('pending', 'leased')
            """
        ).fetchone()
        oldest_value = oldest["oldest"] if oldest is not None else None
        counts["oldest_pending_age"] = (
            max(0.0, time.time() - float(oldest_value)) if oldest_value is not None else None
        )
        counts["legacy_unrecoverable_events"] = self.legacy_failure_count()
        return counts

    def list_legacy_failures(self, limit: int = 10) -> list[LegacyFailure]:
        rows = self._connection.execute(
            """
            SELECT p.source, p.event_id, p.error, p.updated_at
            FROM processed_events AS p
            WHERE p.status IN ('processing', 'failed')
              AND NOT EXISTS (
                  SELECT 1 FROM ingress_events AS e
                  WHERE e.source = p.source AND e.event_id = p.event_id
              )
            ORDER BY p.updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            LegacyFailure(
                source=self._job_source(row["source"]),
                event_id=str(row["event_id"]),
                error=str(row["error"] or "legacy event has no stored payload"),
                updated_at=str(row["updated_at"]),
            )
            for row in rows
        ]

    def legacy_failure_count(self) -> int:
        row = self._connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM processed_events AS p
            WHERE p.status IN ('processing', 'failed')
              AND NOT EXISTS (
                  SELECT 1 FROM ingress_events AS e
                  WHERE e.source = p.source AND e.event_id = p.event_id
              )
            """
        ).fetchone()
        return int(row["count"]) if row is not None else 0

    def health_probe(self) -> bool:
        row = self._connection.execute("PRAGMA quick_check").fetchone()
        if row is None or str(row[0]).lower() != "ok":
            return False
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._connection.execute("UPDATE state SET value = value WHERE 0")
            self._connection.rollback()
        except Exception:
            self._connection.rollback()
            raise
        return True

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
            self._set_processed_event(self._job_source(source), event_id, "done", None)

    def fail_event(self, source: str, event_id: str, error: str) -> None:
        with self._connection:
            self._set_processed_event(self._job_source(source), event_id, "failed", error)

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

    def _select_job(self, job_id: int) -> DeliveryJob | None:
        row = self._connection.execute(
            """
            SELECT j.id, e.source, e.event_id, j.kind, j.conversation_key,
                   e.payload_json, e.telegram_message_id, j.state,
                   j.attempt_count, j.available_at, j.lease_expires_at,
                   j.last_error, j.created_at, j.updated_at
            FROM delivery_jobs AS j
            JOIN ingress_events AS e ON e.id = j.ingress_event_id
            WHERE j.id = ?
            """,
            (job_id,),
        ).fetchone()
        if row is None:
            return None
        return DeliveryJob(
            id=int(row["id"]),
            source=self._job_source(row["source"]),
            event_id=str(row["event_id"]),
            kind=self._job_kind(row["kind"]),
            conversation_key=str(row["conversation_key"]),
            payload_json=str(row["payload_json"]),
            telegram_message_id=(
                int(row["telegram_message_id"]) if row["telegram_message_id"] is not None else None
            ),
            state=self._job_state(row["state"]),
            attempt_count=int(row["attempt_count"]),
            available_at=float(row["available_at"]),
            lease_expires_at=(
                float(row["lease_expires_at"]) if row["lease_expires_at"] is not None else None
            ),
            last_error=str(row["last_error"]) if row["last_error"] is not None else None,
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    def _set_processed_event(
        self,
        source: JobSource,
        event_id: str,
        status: str,
        error: str | None,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO processed_events (source, event_id, status, error)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (source, event_id) DO UPDATE SET
                status = excluded.status,
                error = excluded.error,
                updated_at = CURRENT_TIMESTAMP
            """,
            (source, event_id, status, error),
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

    @staticmethod
    def _job_source(value: object) -> JobSource:
        source = str(value)
        if source not in ("discord", "steam", "telegram"):
            raise RuntimeError(f"Invalid job source in database: {source}")
        return source

    @staticmethod
    def _job_kind(value: object) -> JobKind:
        kind = str(value)
        if kind not in ("route_external_event", "route_telegram_update"):
            raise RuntimeError(f"Invalid job kind in database: {kind}")
        return kind

    @staticmethod
    def _job_state(value: object) -> JobState:
        state = str(value)
        if state not in ("pending", "leased", "succeeded", "failed"):
            raise RuntimeError(f"Invalid job state in database: {state}")
        return state
