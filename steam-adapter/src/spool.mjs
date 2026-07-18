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
    this.remove = this.database.prepare("DELETE FROM pending_events WHERE sequence = ?");
    this.count = this.database.prepare("SELECT COUNT(*) AS count FROM pending_events");
  }

  enqueue(payload) {
    if (typeof payload?.event_id !== "string" || !payload.event_id) {
      throw new TypeError("pending Steam event has no event_id");
    }
    const now = Date.now() / 1000;
    const result = this.insert.run(payload.event_id, JSON.stringify(payload), now, now);
    return result.changes > 0;
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

  size() {
    return Number(this.count.get().count);
  }

  healthProbe() {
    const row = this.database.prepare("PRAGMA quick_check").get();
    if (row?.quick_check !== "ok") return false;
    this.database.exec("BEGIN IMMEDIATE");
    try {
      this.database.exec("UPDATE pending_events SET updated_at = updated_at WHERE 0");
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
