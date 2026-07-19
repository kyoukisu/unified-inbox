import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";

import { PendingEventSpool } from "../src/spool.mjs";

test("pending event spool survives restart and deduplicates", () => {
  const directory = mkdtempSync(join(tmpdir(), "unified-inbox-spool-"));
  const path = join(directory, "steam.sqlite3");
  try {
    const first = new PendingEventSpool(path);
    const payload = { event_id: "steam-message-1", text: "hello" };
    assert.equal(first.enqueue(payload), true);
    assert.equal(first.enqueue(payload), false);
    first.close();

    const second = new PendingEventSpool(path);
    const pending = second.peek();
    assert.equal(pending.eventId, "steam-message-1");
    assert.deepEqual(pending.payload, payload);
    second.failAttempt(pending.sequence, "core offline");
    assert.equal(second.peek().attemptCount, 1);
    second.delete(pending.sequence);
    assert.equal(second.size(), 0);
    assert.equal(second.healthProbe(), true);
    second.close();
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("message enqueue advances a durable cursor without regressing it", () => {
  const directory = mkdtempSync(join(tmpdir(), "unified-inbox-spool-cursor-"));
  const path = join(directory, "steam.sqlite3");
  try {
    const first = new PendingEventSpool(path);
    assert.equal(
      first.enqueueWithHistoryCursor(
        { event_id: "steam-message-2", text: "new" },
        "friend-1",
        200,
        2,
      ),
      true,
    );
    first.advanceHistoryCursor("friend-1", 199, 9);
    first.close();

    const second = new PendingEventSpool(path);
    assert.deepEqual(second.historyCursor("friend-1"), {
      serverTimestamp: 200,
      ordinal: 2,
    });
    assert.deepEqual(second.historyCursors(), [
      { conversationId: "friend-1", serverTimestamp: 200, ordinal: 2 },
    ]);
    second.close();
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("permanently rejected event is quarantined transactionally", () => {
  const directory = mkdtempSync(join(tmpdir(), "unified-inbox-spool-dead-"));
  const path = join(directory, "steam.sqlite3");
  try {
    const spool = new PendingEventSpool(path);
    spool.enqueue({ event_id: "bad-event", text: "bad" });
    const pending = spool.peek();

    assert.equal(spool.quarantine(pending.sequence, "HTTP 400"), true);
    assert.equal(spool.size(), 0);
    assert.equal(spool.deadLetterCount(), 1);
    assert.equal(spool.healthProbe(), true);
    spool.close();
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});
