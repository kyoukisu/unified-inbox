import { compareMessageOrder, messageIdentity } from "./message.mjs";

export function messageCursor(message) {
  const [serverTimestamp, ordinal] = messageIdentity(message).split(":", 2).map(Number);
  return { serverTimestamp, ordinal };
}

export function isAfterCursor(message, cursor) {
  if (!cursor) return true;
  const current = messageCursor(message);
  return current.serverTimestamp > cursor.serverTimestamp
    || (
      current.serverTimestamp === cursor.serverTimestamp
      && current.ordinal > cursor.ordinal
    );
}

export function mergeFriendMessageEvents(events) {
  const unique = new Map();
  for (const event of events) {
    const conversationId = event.message.steamid_friend.toString();
    const key = `${conversationId}:${messageIdentity(event.message)}`;
    if (!unique.has(key)) unique.set(key, event);
  }
  return [...unique.values()].sort((left, right) => {
    const order = compareMessageOrder(left.message, right.message);
    if (order !== 0) return order;
    return left.message.steamid_friend.toString().localeCompare(
      right.message.steamid_friend.toString(),
    );
  });
}

export async function collectHistoryPages(fetchPage, options) {
  const messages = new Map();
  let lastBoundary = null;
  let lastTime;
  let lastOrdinal;

  while (true) {
    const page = await fetchPage({ ...options, lastTime, lastOrdinal });
    const pageMessages = page.messages ?? [];
    for (const message of pageMessages) {
      messages.set(messageIdentity(message), message);
    }
    if (!page.more_available) break;
    if (pageMessages.length === 0) {
      throw new Error("Steam history pagination reported more messages without a boundary");
    }

    const oldest = [...pageMessages].sort(compareMessageOrder)[0];
    const boundary = messageIdentity(oldest);
    if (boundary === lastBoundary) {
      throw new Error(`Steam history pagination did not advance past ${boundary}`);
    }
    lastBoundary = boundary;
    lastTime = oldest.server_timestamp;
    lastOrdinal = oldest.ordinal ?? 0;
  }

  return [...messages.values()].sort(compareMessageOrder);
}
