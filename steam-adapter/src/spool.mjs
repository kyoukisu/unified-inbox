import { mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { DatabaseSync } from "node:sqlite";

export class PendingEventSpool {
  constructor(path) {
    mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
    this.database = new DatabaseSync(path);
    this.database.exec(`
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

      CREATE TABLE IF NOT EXISTS steam_history_cursors (
        conversation_id TEXT PRIMARY KEY,
        server_timestamp INTEGER NOT NULL,
        ordinal INTEGER NOT NULL,
        updated_at REAL NOT NULL
      );

      CREATE TABLE IF NOT EXISTS dead_letter_events (
        sequence INTEGER PRIMARY KEY,
        original_sequence INTEGER NOT NULL,
        event_id TEXT NOT NULL UNIQUE,
        payload_json TEXT NOT NULL,
        attempt_count INTEGER NOT NULL,
        last_error TEXT NOT NULL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        quarantined_at REAL NOT NULL
      );
    `);
    this.insert = this.database.prepare(`
      INSERT OR IGNORE INTO pending_events (
        event_id, payload_json, attempt_count, last_error, created_at, updated_at
      ) VALUES (?, ?, 0, NULL, ?, ?)
    `);
    this.selectNext = this.database.prepare(`
      SELECT sequence, event_id, payload_json, attempt_count
      FROM pending_events
      ORDER BY sequence
      LIMIT 1
    `);
    this.markFailed = this.database.prepare(`
      UPDATE pending_events
      SET attempt_count = attempt_count + 1, last_error = ?, updated_at = ?
      WHERE sequence = ?
    `);
    this.upsertCursor = this.database.prepare(`
      INSERT INTO steam_history_cursors (
        conversation_id, server_timestamp, ordinal, updated_at
      ) VALUES (?, ?, ?, ?)
      ON CONFLICT(conversation_id) DO UPDATE SET
        server_timestamp = excluded.server_timestamp,
        ordinal = excluded.ordinal,
        updated_at = excluded.updated_at
      WHERE excluded.server_timestamp > steam_history_cursors.server_timestamp
         OR (
           excluded.server_timestamp = steam_history_cursors.server_timestamp
           AND excluded.ordinal > steam_history_cursors.ordinal
         )
    `);
    this.selectCursor = this.database.prepare(`
      SELECT server_timestamp, ordinal
      FROM steam_history_cursors
      WHERE conversation_id = ?
    `);
    this.selectCursors = this.database.prepare(`
      SELECT conversation_id, server_timestamp, ordinal
      FROM steam_history_cursors
      ORDER BY conversation_id
    `);
    this.quarantinePending = this.database.prepare(`
      INSERT OR IGNORE INTO dead_letter_events (
        original_sequence, event_id, payload_json, attempt_count, last_error,
        created_at, updated_at, quarantined_at
      )
      SELECT sequence, event_id, payload_json, attempt_count, ?,
             created_at, ?, ?
      FROM pending_events
      WHERE sequence = ?
    `);
    this.remove = this.database.prepare("DELETE FROM pending_events WHERE sequence = ?");
    this.count = this.database.prepare("SELECT COUNT(*) AS count FROM pending_events");
    this.deadLetterCountStatement = this.database.prepare(
      "SELECT COUNT(*) AS count FROM dead_letter_events",
    );
  }

  enqueue(payload) {
    if (typeof payload?.event_id !== "string" || !payload.event_id) {
      throw new TypeError("pending Steam event has no event_id");
    }
    const now = Date.now() / 1000;
    const result = this.insert.run(payload.event_id, JSON.stringify(payload), now, now);
    return result.changes > 0;
  }

  enqueueWithHistoryCursor(payload, conversationId, serverTimestamp, ordinal) {
    if (typeof payload?.event_id !== "string" || !payload.event_id) {
      throw new TypeError("pending Steam event has no event_id");
    }
    if (typeof conversationId !== "string" || !conversationId) {
      throw new TypeError("Steam history cursor has no conversation_id");
    }
    if (!Number.isInteger(serverTimestamp) || !Number.isInteger(ordinal)) {
      throw new TypeError("Steam history cursor must contain integer timestamp and ordinal");
    }

    const now = Date.now() / 1000;
    const payloadJson = JSON.stringify(payload);
    this.database.exec("BEGIN IMMEDIATE");
    try {
      const result = this.insert.run(payload.event_id, payloadJson, now, now);
      this.upsertCursor.run(conversationId, serverTimestamp, ordinal, now);
      this.database.exec("COMMIT");
      return result.changes > 0;
    } catch (error) {
      try {
        this.database.exec("ROLLBACK");
      } catch {
        // The original transaction error is the useful failure.
      }
      throw error;
    }
  }

  historyCursor(conversationId) {
    const row = this.selectCursor.get(conversationId);
    if (!row) return null;
    return {
      serverTimestamp: Number(row.server_timestamp),
      ordinal: Number(row.ordinal),
    };
  }

  historyCursors() {
    return this.selectCursors.all().map((row) => ({
      conversationId: String(row.conversation_id),
      serverTimestamp: Number(row.server_timestamp),
      ordinal: Number(row.ordinal),
    }));
  }

  advanceHistoryCursor(conversationId, serverTimestamp, ordinal) {
    if (typeof conversationId !== "string" || !conversationId) {
      throw new TypeError("Steam history cursor has no conversation_id");
    }
    if (!Number.isInteger(serverTimestamp) || !Number.isInteger(ordinal)) {
      throw new TypeError("Steam history cursor must contain integer timestamp and ordinal");
    }
    this.upsertCursor.run(
      conversationId,
      serverTimestamp,
      ordinal,
      Date.now() / 1000,
    );
  }

  peek() {
    const row = this.selectNext.get();
    if (!row) return null;
    const payload = JSON.parse(row.payload_json);
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
      throw new TypeError("stored Steam event payload is not an object");
    }
    return {
      sequence: Number(row.sequence),
      eventId: String(row.event_id),
      payload,
      attemptCount: Number(row.attempt_count),
    };
  }

  failAttempt(sequence, error) {
    this.markFailed.run(String(error), Date.now() / 1000, sequence);
  }

  delete(sequence) {
    this.remove.run(sequence);
  }

  quarantine(sequence, error) {
    const now = Date.now() / 1000;
    this.database.exec("BEGIN IMMEDIATE");
    try {
      this.quarantinePending.run(String(error), now, now, sequence);
      const result = this.remove.run(sequence);
      this.database.exec("COMMIT");
      return result.changes > 0;
    } catch (transactionError) {
      try {
        this.database.exec("ROLLBACK");
      } catch {
        // The original transaction error is the useful failure.
      }
      throw transactionError;
    }
  }

  size() {
    return Number(this.count.get().count);
  }

  deadLetterCount() {
    return Number(this.deadLetterCountStatement.get().count);
  }

  healthProbe() {
    const row = this.database.prepare("PRAGMA quick_check").get();
    if (row?.quick_check !== "ok") return false;
    this.database.exec("BEGIN IMMEDIATE");
    try {
      this.database.exec(`
        UPDATE pending_events SET updated_at = updated_at WHERE 0;
        UPDATE dead_letter_events SET updated_at = updated_at WHERE 0;
        UPDATE steam_history_cursors SET updated_at = updated_at WHERE 0;
      `);
      this.database.exec("ROLLBACK");
    } catch (error) {
      try {
        this.database.exec("ROLLBACK");
      } catch {
        // The original write error is the useful failure.
      }
      throw error;
    }
    return true;
  }

  close() {
    this.database.close();
  }
}
