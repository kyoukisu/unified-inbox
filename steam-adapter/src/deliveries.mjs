import { readFile } from "node:fs/promises";

import { atomicWritePrivate } from "./files.mjs";

function normalizeRecord(value) {
  if (typeof value === "string") {
    return {
      conversationId: null,
      imageUrl: null,
      textMessageId: null,
      messageId: value,
      completed: true,
    };
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  return {
    conversationId: typeof value.conversationId === "string" ? value.conversationId : null,
    imageUrl: typeof value.imageUrl === "string" ? value.imageUrl : null,
    textMessageId: typeof value.textMessageId === "string" ? value.textMessageId : null,
    messageId: typeof value.messageId === "string" ? value.messageId : null,
    completed: value.completed === true,
  };
}

export class DeliveryStore {
  constructor(path, limit = 5000) {
    this.path = path;
    this.limit = limit;
    this.deliveries = new Map();
    this.persistChain = Promise.resolve();
  }

  async load() {
    try {
      const parsed = JSON.parse(await readFile(this.path, "utf8"));
      if (!Array.isArray(parsed)) {
        throw new TypeError("delivery store must be an array");
      }
      for (const item of parsed) {
        if (Array.isArray(item) && item.length === 2) {
          const record = normalizeRecord(item[1]);
          if (record) this.deliveries.set(String(item[0]), record);
        }
      }
    } catch (error) {
      if (error?.code !== "ENOENT") {
        throw new Error("Unable to load Steam delivery store", { cause: error });
      }
    }
  }

  get(idempotencyKey) {
    const record = this.deliveries.get(idempotencyKey);
    return record?.completed ? record.messageId : undefined;
  }

  getRecord(idempotencyKey) {
    const record = this.deliveries.get(idempotencyKey);
    return record ? { ...record } : null;
  }

  hasMessageId(conversationId, messageId) {
    return [...this.deliveries.values()].some(
      (record) =>
        record.conversationId === conversationId
        && (record.textMessageId === messageId || record.messageId === messageId),
    );
  }

  hasImageUrl(conversationId, imageUrl) {
    return [...this.deliveries.values()].some(
      (record) => record.conversationId === conversationId && record.imageUrl === imageUrl,
    );
  }

  async update(idempotencyKey, patch) {
    const current = this.deliveries.get(idempotencyKey) ?? {
      conversationId: null,
      imageUrl: null,
      textMessageId: null,
      messageId: null,
      completed: false,
    };
    this.deliveries.delete(idempotencyKey);
    this.deliveries.set(idempotencyKey, { ...current, ...patch });
    this.#prune();
    await this.#persist();
  }

  async set(idempotencyKey, messageId) {
    await this.update(idempotencyKey, { messageId, completed: true });
  }

  #prune() {
    while (this.deliveries.size > this.limit) {
      const completed = [...this.deliveries].find(([, record]) => record.completed);
      if (!completed) return;
      this.deliveries.delete(completed[0]);
    }
  }

  async #persist() {
    const serialized = JSON.stringify([...this.deliveries]);
    this.persistChain = this.persistChain
      .catch(() => undefined)
      .then(() => atomicWritePrivate(this.path, serialized));
    await this.persistChain;
  }
}
