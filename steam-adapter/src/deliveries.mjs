import { readFile } from "node:fs/promises";

import { atomicWritePrivate } from "./files.mjs";

export class DeliveryStore {
  constructor(path, limit = 2000) {
    this.path = path;
    this.limit = limit;
    this.deliveries = new Map();
  }

  async load() {
    try {
      const parsed = JSON.parse(await readFile(this.path, "utf8"));
      if (!Array.isArray(parsed)) {
        throw new TypeError("delivery store must be an array");
      }
      for (const item of parsed) {
        if (Array.isArray(item) && item.length === 2) {
          this.deliveries.set(String(item[0]), String(item[1]));
        }
      }
    } catch (error) {
      if (error?.code !== "ENOENT") {
        throw new Error("Unable to load Steam delivery store", { cause: error });
      }
    }
  }

  get(idempotencyKey) {
    return this.deliveries.get(idempotencyKey);
  }

  async set(idempotencyKey, messageId) {
    this.deliveries.delete(idempotencyKey);
    this.deliveries.set(idempotencyKey, messageId);
    while (this.deliveries.size > this.limit) {
      const oldest = this.deliveries.keys().next().value;
      this.deliveries.delete(oldest);
    }
    await atomicWritePrivate(this.path, JSON.stringify([...this.deliveries]));
  }
}
