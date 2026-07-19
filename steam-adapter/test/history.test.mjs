import test from "node:test";
import assert from "node:assert/strict";

import {
  collectHistoryPages,
  isAfterCursor,
  mergeFriendMessageEvents,
} from "../src/history.mjs";
import { messageIdentity } from "../src/message.mjs";

function message(second, ordinal, conversationId = "friend") {
  return {
    steamid_friend: { toString: () => conversationId },
    server_timestamp: new Date(`2026-07-15T10:00:${String(second).padStart(2, "0")}Z`),
    ordinal,
  };
}

test("collects newest-first history pages in chronological order", async () => {
  const calls = [];
  const pages = [
    { messages: [message(4, 0), message(3, 0)], more_available: true },
    { messages: [message(3, 0), message(2, 1)], more_available: true },
    { messages: [message(2, 0)], more_available: false },
  ];

  const history = await collectHistoryPages(async (options) => {
    calls.push(options);
    return pages.shift();
  }, { maxCount: 100 });

  assert.deepEqual(history.map(messageIdentity), [
    "1784109602:0",
    "1784109602:1",
    "1784109603:0",
    "1784109604:0",
  ]);
  assert.equal(calls[1].lastOrdinal, 0);
  assert.equal(calls[1].lastTime.toISOString(), "2026-07-15T10:00:03.000Z");
});

test("rejects a non-advancing history boundary", async () => {
  const repeated = { messages: [message(3, 0)], more_available: true };

  await assert.rejects(
    collectHistoryPages(async () => repeated, { maxCount: 100 }),
    /did not advance/,
  );
});

test("merges duplicate live and history events in chronological order", () => {
  const older = { message: message(1, 0, "b"), direction: "inbound" };
  const newer = { message: message(2, 0, "a"), direction: "outbound_native" };

  const merged = mergeFriendMessageEvents([newer, older, newer]);

  assert.deepEqual(merged, [older, newer]);
});

test("compares a message against a durable cursor", () => {
  const cursor = { serverTimestamp: 1784109602, ordinal: 1 };

  assert.equal(isAfterCursor(message(2, 1), cursor), false);
  assert.equal(isAfterCursor(message(2, 2), cursor), true);
  assert.equal(isAfterCursor(message(3, 0), cursor), true);
});
