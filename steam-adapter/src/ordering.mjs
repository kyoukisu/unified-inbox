function orderParts(orderKey) {
  const [timestamp, ordinal] = String(orderKey).split(":", 2).map(Number);
  return [timestamp, ordinal];
}

export class ConversationEventGate {
  constructor(deliver) {
    this.deliver = deliver;
    this.holds = new Map();
    this.pending = new Map();
    this.sequence = 0;
  }

  hold(conversationId) {
    this.holds.set(conversationId, (this.holds.get(conversationId) ?? 0) + 1);
  }

  release(conversationId) {
    const count = this.holds.get(conversationId) ?? 0;
    if (count > 1) {
      this.holds.set(conversationId, count - 1);
      return;
    }
    this.holds.delete(conversationId);
    const pending = this.pending.get(conversationId) ?? [];
    this.pending.delete(conversationId);
    pending.sort((left, right) => {
      const [leftTimestamp, leftOrdinal] = orderParts(left.orderKey);
      const [rightTimestamp, rightOrdinal] = orderParts(right.orderKey);
      return (
        leftTimestamp - rightTimestamp
        || leftOrdinal - rightOrdinal
        || left.sequence - right.sequence
      );
    });
    for (const item of pending) this.deliver(item.value);
  }

  observe(conversationId, orderKey, value) {
    if (!this.holds.has(conversationId)) {
      this.deliver(value);
      return;
    }
    this.sequence += 1;
    const pending = this.pending.get(conversationId) ?? [];
    pending.push({ orderKey, sequence: this.sequence, value });
    this.pending.set(conversationId, pending);
  }
}
