import { readFile } from "node:fs/promises";

import { atomicWritePrivate } from "./files.mjs";

function normalizeRecord(value) {
  if (typeof value === "string") {
    return {
      conversationId: null,
      imageUrl: null,
      textMessageId: null,
      messageId: value,
      textStartedAt: null,
      text: null,
      imageStartedAt: null,
      imageSha: null,
      updatedAt: Date.now(),
      completed: true,
    };
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  return {
    conversationId: typeof value.conversationId === "string" ? value.conversationId : null,
    imageUrl: typeof value.imageUrl === "string" ? value.imageUrl : null,
    textMessageId: typeof value.textMessageId === "string" ? value.textMessageId : null,
    messageId: typeof value.messageId === "string" ? value.messageId : null,
    textStartedAt: Number.isFinite(value.textStartedAt) ? value.textStartedAt : null,
    text: typeof value.text === "string" ? value.text : null,
    imageStartedAt: Number.isFinite(value.imageStartedAt) ? value.imageStartedAt : null,
    imageSha: typeof value.imageSha === "string" ? value.imageSha : null,
    updatedAt: Number.isFinite(value.updatedAt) ? value.updatedAt : Date.now(),
    completed: value.completed === true,
  };
}

export class DeliveryStore {
  constructor(path, limit = 5000, retentionMilliseconds = 16 * 24 * 60 * 60 * 1000) {
    this.path = path;
    this.limit = limit;
    this.retentionMilliseconds = retentionMilliseconds;
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

  ambiguousRecords() {
    return [...this.deliveries]
      .filter(
        ([, record]) =>
          !record.completed
          && (
            (record.textStartedAt && !record.textMessageId)
            || (record.imageStartedAt && !record.imageUrl)
          ),
      )
      .map(([idempotencyKey, record]) => ({
        idempotencyKey,
        record: { ...record },
      }));
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
      textStartedAt: null,
      text: null,
      imageStartedAt: null,
      imageSha: null,
      updatedAt: 0,
      completed: false,
    };
    this.deliveries.delete(idempotencyKey);
    this.deliveries.set(idempotencyKey, {
      ...current,
      ...patch,
      updatedAt: Date.now(),
    });
    this.#prune();
    await this.#persist();
  }

  async set(idempotencyKey, messageId) {
    await this.update(idempotencyKey, { messageId, completed: true });
  }

  #prune() {
    const cutoff = Date.now() - this.retentionMilliseconds;
    for (const [key, record] of this.deliveries) {
      if (record.completed && record.updatedAt < cutoff) {
        this.deliveries.delete(key);
      }
    }
    if (this.deliveries.size <= this.limit) return;
    const completed = [...this.deliveries]
      .filter(([, record]) => record.completed)
      .sort((left, right) => left[1].updatedAt - right[1].updatedAt);
    for (const [key, record] of completed) {
      if (this.deliveries.size <= this.limit || record.updatedAt >= cutoff) break;
      this.deliveries.delete(key);
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
