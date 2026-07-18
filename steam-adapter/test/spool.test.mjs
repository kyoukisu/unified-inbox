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
