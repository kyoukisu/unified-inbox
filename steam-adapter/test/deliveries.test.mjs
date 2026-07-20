import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";

import { DeliveryStore } from "../src/deliveries.mjs";

test("delivery store persists idempotency mappings", async () => {
  const directory = await mkdtemp(join(tmpdir(), "unified-inbox-"));
  try {
    const path = join(directory, "deliveries.json");
    const first = new DeliveryStore(path, 2);
    await first.load();
    await first.set("one", "message-1");
    await first.set("two", "message-2");
    await first.set("three", "message-3");

    const second = new DeliveryStore(path, 2);
    await second.load();
    assert.equal(second.get("one"), "message-1");
    assert.equal(second.get("two"), "message-2");
    assert.equal(second.get("three"), "message-3");

    await second.update("partial", {
      conversationId: "steam-alice",
      imageUrl: "https://steam.example/image",
      textMessageId: "1700000000:1",
      textStartedAt: 1234,
      text: "hello",
      imageStartedAt: 1235,
      imageSha: "abc123",
    });
    await second.update("ambiguous", {
      conversationId: "steam-alice",
      textStartedAt: 2000,
      text: "pending",
    });
    const third = new DeliveryStore(path, 3);
    await third.load();
    const partial = third.getRecord("partial");
    assert.equal(partial.conversationId, "steam-alice");
    assert.equal(partial.imageUrl, "https://steam.example/image");
    assert.equal(partial.textMessageId, "1700000000:1");
    assert.equal(partial.textStartedAt, 1234);
    assert.equal(partial.text, "hello");
    assert.equal(partial.imageStartedAt, 1235);
    assert.equal(partial.imageSha, "abc123");
    assert.equal(partial.messageId, null);
    assert.equal(partial.completed, false);
    assert.ok(partial.updatedAt > 0);
    assert.deepEqual(third.ambiguousRecords().map((item) => item.idempotencyKey), [
      "ambiguous",
    ]);
    assert.equal(third.hasMessageId("steam-alice", "1700000000:1"), true);
    assert.equal(third.hasMessageId("steam-bob", "1700000000:1"), false);
    assert.equal(third.hasMessageId("steam-alice", "missing"), false);
    assert.equal(third.hasImageUrl("steam-alice", "https://steam.example/image"), true);
    assert.equal(third.hasImageUrl("steam-bob", "https://steam.example/image"), false);
    assert.equal(third.hasImageUrl("steam-alice", "https://steam.example/missing"), false);
    assert.ok((await readFile(path, "utf8")).length > 0);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});
