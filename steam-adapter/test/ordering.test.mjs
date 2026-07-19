import test from "node:test";
import assert from "node:assert/strict";

import { ConversationEventGate } from "../src/ordering.mjs";

test("buffers a fast reply until the earlier outbound echo is classified", () => {
  const delivered = [];
  const gate = new ConversationEventGate((event) => delivered.push(event));

  gate.hold("alice");
  gate.observe("alice", "1700000001:0", "reply-second");
  gate.observe("alice", "1700000000:0", "outbound-first");
  assert.deepEqual(delivered, []);

  gate.release("alice");
  assert.deepEqual(delivered, ["outbound-first", "reply-second"]);
});

test("keeps conversations independent and supports nested sends", () => {
  const delivered = [];
  const gate = new ConversationEventGate((event) => delivered.push(event));

  gate.hold("alice");
  gate.hold("alice");
  gate.observe("alice", "1700000000:1", "alice");
  gate.observe("bob", "1700000000:0", "bob");
  gate.release("alice");
  assert.deepEqual(delivered, ["bob"]);
  gate.release("alice");
  assert.deepEqual(delivered, ["bob", "alice"]);
});
