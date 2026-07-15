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
    assert.equal(second.get("one"), undefined);
    assert.equal(second.get("two"), "message-2");
    assert.equal(second.get("three"), "message-3");
    assert.ok((await readFile(path, "utf8")).length > 0);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});
