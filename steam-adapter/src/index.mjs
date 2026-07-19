import { randomUUID } from "node:crypto";
import { createServer } from "node:http";

import SteamUser from "steam-user";

import { loadConfig } from "./config.mjs";
import { DeliveryStore } from "./deliveries.mjs";
import { atomicWritePrivate, readRequiredFile } from "./files.mjs";
import {
  collectHistoryPages,
  isAfterCursor,
  mergeFriendMessageEvents,
  messageCursor,
} from "./history.mjs";
import { messageIdentity, parseFriendMessage } from "./message.mjs";
import { ConversationEventGate } from "./ordering.mjs";
import { normalizeSteamPresence } from "./presence.mjs";
import { PendingEventSpool } from "./spool.mjs";
import { SteamImageUploader } from "./steam-image.mjs";

const config = await loadConfig();
const refreshToken = await readRequiredFile(config.refreshTokenFile, "Steam refresh token");
const deliveries = new DeliveryStore(config.deliveriesFile);
await deliveries.load();
const spool = new PendingEventSpool(config.spoolDatabase);

const client = new SteamUser({ autoRelogin: true, renewRefreshTokens: true });
const imageUploader = new SteamImageUploader(client);
let connected = false;
let accountId = null;
let shuttingDown = false;
let spoolAlive = true;
let spoolError = null;
let spoolClosed = false;
const bridgeMessageIds = new Set();
const bridgeImageUrls = new Set();
const outboundLocks = new Map();
const lastPresence = new Map();
const knownDisplayNames = new Map();
const bufferedFriendMessages = [];
const presenceSessionId = randomUUID();
let presenceSequence = 0;
let personaRefreshTimer = null;
let personaRefreshRunning = false;
let reconnectWatchdog = null;
let historySyncPending = true;
let historySyncRunning = false;
const messageOrderGate = new ConversationEventGate(({
  message,
  direction,
  advanceCursor,
}) => {
  handleFriendMessage(message, direction, advanceCursor);
});

function rememberBridgeValue(values, value) {
  if (values.size >= 2048) values.delete(values.values().next().value);
  values.add(value);
}

function consumeBridgeValue(values, value) {
  if (!values.has(value)) return false;
  values.delete(value);
  return true;
}

function log(level, message, error = null) {
  const line = `${new Date().toISOString()} ${level} ${message}`;
  if (error) console.error(line, error instanceof Error ? error.message : String(error));
  else console.log(line);
}

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function closeSpool() {
  if (spoolClosed) return;
  spoolClosed = true;
  spool.close();
}

function stopForSpoolFailure(error) {
  spoolAlive = false;
  spoolError = error instanceof Error ? error.message : String(error);
  shuttingDown = true;
  process.exitCode = 1;
  server.close();
  client.logOff();
  closeSpool();
}

async function loadKnownSteamConversations() {
  const response = await fetch(`${config.coreUrl}/v1/conversations/steam`, {
    headers: { Authorization: `Bearer ${config.internalToken}` },
    signal: AbortSignal.timeout(10000),
  });
  if (!response.ok) throw new Error(`core returned HTTP ${response.status}`);
  const payload = await response.json();
  if (!Array.isArray(payload?.conversations)) {
    throw new TypeError("core conversations response is invalid");
  }
  return payload.conversations.filter(
    (conversation) =>
      typeof conversation?.conversation_id === "string"
      && conversation.conversation_id.length > 0
      && typeof conversation?.display_name === "string"
      && conversation.display_name.length > 0,
  );
}

async function refreshKnownSteamPersonas() {
  if (!connected || personaRefreshRunning) return;
  personaRefreshRunning = true;
  try {
    const conversations = await loadKnownSteamConversations();
    for (const conversation of conversations) {
      knownDisplayNames.set(conversation.conversation_id, conversation.display_name);
    }
    if (conversations.length === 0) return;
    try {
      const result = await client.getPersonas(
        conversations.map((conversation) => conversation.conversation_id),
      );
      for (const [steamId, user] of Object.entries(result.personas ?? {})) {
        handleSteamPresence(steamId, user);
      }
    } catch (error) {
      log("WARN", "Unable to refresh one or more known Steam personas", error);
    }
    for (const conversation of conversations) {
      if (!lastPresence.has(conversation.conversation_id)) {
        handleSteamPresence(conversation.conversation_id, {
          persona_state: SteamUser.EPersonaState.Offline,
          player_name: conversation.display_name,
        });
      }
    }
  } catch (error) {
    log("WARN", "Unable to load known Steam conversations", error);
  } finally {
    personaRefreshRunning = false;
  }
}

function startPersonaRefresh() {
  if (personaRefreshTimer) clearInterval(personaRefreshTimer);
  void refreshKnownSteamPersonas();
  personaRefreshTimer = setInterval(() => {
    void refreshKnownSteamPersonas();
  }, 60000);
}

async function reconcileRecentFriendMessages() {
  if (!connected || historySyncRunning) return;
  historySyncRunning = true;
  const bootstrapSince = new Date(Date.now() - 24 * 60 * 60 * 1000);
  const cursors = spool.historyCursors();
  const discoverySince = cursors.length > 0
    ? new Date(Math.min(...cursors.map((cursor) => cursor.serverTimestamp)) * 1000)
    : bootstrapSince;
  const historyEvents = [];
  const failedConversations = new Set();
  let discoveryFailed = false;
  try {
    const active = await client.chat.getActiveFriendMessageSessions({
      conversationsSince: discoverySince,
    });
    for (const session of active.sessions ?? []) {
      if (!connected) {
        discoveryFailed = true;
        break;
      }
      const conversationId = session.steamid_friend.toString();
      const cursor = spool.historyCursor(conversationId);
      try {
        const messages = await collectHistoryPages(
          (options) => client.chat.getFriendMessageHistory(session.steamid_friend, options),
          {
            maxCount: 100,
            startTime: cursor
              ? new Date(cursor.serverTimestamp * 1000)
              : bootstrapSince,
            startOrdinal: cursor?.ordinal,
            wantBbcode: true,
          },
        );
        for (const message of messages) {
          if (!isAfterCursor(message, cursor)) continue;
          const senderId = message.sender?.toString();
          if (!senderId) continue;
          historyEvents.push({
            message: { ...message, steamid_friend: session.steamid_friend },
            direction: senderId === accountId ? "outbound_native" : "inbound",
          });
        }
      } catch (error) {
        failedConversations.add(conversationId);
        log("WARN", `Unable to reconcile Steam history for ${conversationId}`, error);
      }
    }
  } catch (error) {
    discoveryFailed = true;
    log("WARN", "Unable to load recent Steam message sessions", error);
  } finally {
    const buffered = bufferedFriendMessages.splice(0);
    const merged = mergeFriendMessageEvents([...historyEvents, ...buffered]);
    historySyncRunning = false;
    historySyncPending = false;
    for (const event of merged) {
      const conversationId = event.message.steamid_friend.toString();
      releaseFriendMessage(
        event.message,
        event.direction,
        !discoveryFailed && !failedConversations.has(conversationId),
      );
    }
    log("INFO", `Steam history reconciliation observed ${historyEvents.length} messages`);
  }
}

function exitForSteamFailure(message, error = null) {
  if (shuttingDown) return;
  log("ERROR", message, error);
  connected = false;
  shuttingDown = true;
  process.exitCode = 1;
  setTimeout(() => process.exit(1), 250);
}

client.on("loggedOn", () => {
  connected = true;
  historySyncPending = true;
  accountId = client.steamID?.getSteamID64() ?? null;
  client.setPersona(SteamUser.EPersonaState.Invisible);
  if (reconnectWatchdog) clearTimeout(reconnectWatchdog);
  reconnectWatchdog = null;
  startPersonaRefresh();
  setTimeout(() => void reconcileRecentFriendMessages(), 2000);
  log("INFO", `Steam client connected as ${accountId}`);
});
client.on("disconnected", (result, message) => {
  connected = false;
  historySyncPending = true;
  if (personaRefreshTimer) clearInterval(personaRefreshTimer);
  personaRefreshTimer = null;
  if (reconnectWatchdog) clearTimeout(reconnectWatchdog);
  reconnectWatchdog = setTimeout(
    () => exitForSteamFailure("Steam reconnect watchdog expired"),
    120000,
  );
  log("WARN", `Steam client disconnected (${result}): ${message}`);
});
client.on("error", (error) => exitForSteamFailure("Steam client error", error));
client.on("refreshToken", async (token) => {
  try {
    await atomicWritePrivate(config.refreshTokenFile, token);
    log("INFO", "Steam refresh token rotated and saved");
  } catch (error) {
    log("ERROR", "Unable to persist rotated Steam refresh token", error);
  }
});
client.on("user", (steamId, user) => {
  handleSteamPresence(steamId, user);
});
client.on("friendPersonasLoaded", () => {
  for (const [steamId, user] of Object.entries(client.users)) {
    handleSteamPresence(steamId, user);
  }
});

client.chat.on("friendMessage", (message) => {
  observeFriendMessage(message, "inbound");
});
client.chat.on("friendMessageEcho", (message) => {
  observeFriendMessage(message, "outbound_native");
});

function handleSteamPresence(steamIdValue, user) {
  const steamId = steamIdValue.toString();
  if (steamId === accountId) return;
  const status = normalizeSteamPresence(user?.persona_state);
  if (!status) return;
  const displayName = user?.player_name
    ?? client.users[steamId]?.player_name
    ?? knownDisplayNames.get(steamId);
  if (!displayName) return;
  const current = `${status}\0${displayName}`;
  if (lastPresence.get(steamId) === current) return;

  presenceSequence += 1;
  const eventId = `presence:${steamId}:${presenceSessionId}:${presenceSequence}`;
  try {
    spool.enqueue({
      kind: "presence",
      platform: "steam",
      event_id: eventId,
      conversation_id: steamId,
      display_name: displayName,
      status,
    });
    lastPresence.set(steamId, current);
  } catch (error) {
    log("ERROR", `Unable to persist Steam presence ${eventId}`, error);
    stopForSpoolFailure(error);
  }
}

function observeFriendMessage(message, direction) {
  if (historySyncPending || historySyncRunning) {
    bufferedFriendMessages.push({ message, direction });
    return;
  }
  releaseFriendMessage(message, direction, true);
}

function releaseFriendMessage(message, direction, advanceCursor) {
  const steamId = message.steamid_friend.toString();
  messageOrderGate.observe(steamId, messageIdentity(message), {
    message,
    direction,
    advanceCursor,
  });
}

function handleFriendMessage(message, direction, advanceCursor = true) {
  const steamId = message.steamid_friend.toString();
  const user = client.users[steamId];
  if (user) handleSteamPresence(steamId, user);
  const identity = messageIdentity(message);
  const cursor = messageCursor(message);
  try {
    const { text, attachments } = parseFriendMessage(message);
    const bridgeEcho = direction === "outbound_native" && (
      consumeBridgeValue(bridgeMessageIds, identity)
      || deliveries.hasMessageId(steamId, identity)
      || attachments.some(
        (attachment) =>
          consumeBridgeValue(bridgeImageUrls, attachment.url)
          || deliveries.hasImageUrl(steamId, attachment.url)
      )
    );
    if (bridgeEcho || (!text && attachments.length === 0)) {
      if (advanceCursor) {
        spool.advanceHistoryCursor(
          steamId,
          cursor.serverTimestamp,
          cursor.ordinal,
        );
      }
      return;
    }

    const displayName = client.users[steamId]?.player_name
      ?? knownDisplayNames.get(steamId)
      ?? steamId;
    const payload = {
      platform: "steam",
      event_id: `${steamId}:${identity}`,
      conversation_id: steamId,
      display_name: displayName,
      sender_id: direction === "outbound_native" ? (accountId ?? "self") : steamId,
      sender_name: direction === "outbound_native" ? "You" : displayName,
      message_id: identity,
      text,
      reply_to_message_id: null,
      attachments,
      direction,
    };
    if (advanceCursor) {
      spool.enqueueWithHistoryCursor(
        payload,
        steamId,
        cursor.serverTimestamp,
        cursor.ordinal,
      );
    } else {
      spool.enqueue(payload);
    }
  } catch (error) {
    log("ERROR", `Unable to persist observed Steam event ${steamId}:${identity}`, error);
    stopForSpoolFailure(error);
  }
}

async function deliverPendingEvents() {
  let delay = 1000;
  while (!shuttingDown) {
    const pending = spool.peek();
    if (!pending) {
      await sleep(500);
      continue;
    }
    try {
      const response = await fetch(`${config.coreUrl}/v1/events`, {
        method: "POST",
        headers: {
          authorization: `Bearer ${config.internalToken}`,
          "content-type": "application/json",
        },
        body: JSON.stringify(pending.payload),
        signal: AbortSignal.timeout(90_000),
      });
      if (!response.ok) {
        const error = `core returned HTTP ${response.status}: ${(
          await response.text()
        ).slice(0, 300)}`;
        if (response.status === 400) {
          spool.quarantine(pending.sequence, error);
          log("ERROR", `Steam event ${pending.eventId} moved to dead letter`, error);
          delay = 1000;
          continue;
        }
        throw new Error(error);
      }
      spool.delete(pending.sequence);
      delay = 1000;
    } catch (error) {
      spool.failAttempt(pending.sequence, error instanceof Error ? error.message : String(error));
      log("WARN", `Steam event ${pending.eventId} remains queued`, error);
      await sleep(delay);
      delay = Math.min(delay * 2, 30_000);
    }
  }
}

async function withOutboundLock(key, operation) {
  const previous = outboundLocks.get(key) ?? Promise.resolve();
  const current = previous.catch(() => undefined).then(operation);
  outboundLocks.set(key, current);
  try {
    return await current;
  } finally {
    if (outboundLocks.get(key) === current) outboundLocks.delete(key);
  }
}

async function sendOutbound(metadata, image) {
  return withOutboundLock(metadata.conversation_id, async () => {
    const cached = deliveries.get(metadata.idempotency_key);
    if (cached) return cached;
    if (!connected) throw new Error("Steam client is not connected");

    messageOrderGate.hold(metadata.conversation_id);
    try {
      let record = deliveries.getRecord(metadata.idempotency_key) ?? {
        conversationId: metadata.conversation_id,
        imageUrl: null,
        textMessageId: null,
        messageId: null,
        completed: false,
      };

      if (image && !record.imageUrl) {
        const imageUrl = await imageUploader.sendImageToUser(metadata.conversation_id, image);
        rememberBridgeValue(bridgeImageUrls, imageUrl);
        await deliveries.update(metadata.idempotency_key, {
          conversationId: metadata.conversation_id,
          imageUrl,
        });
        record = deliveries.getRecord(metadata.idempotency_key);
      }

      if (metadata.text && !record.textMessageId) {
        const sent = await client.chat.sendFriendMessage(metadata.conversation_id, metadata.text, {
          containsBbCode: false,
        });
        const textMessageId = messageIdentity(sent);
        rememberBridgeValue(bridgeMessageIds, textMessageId);
        await deliveries.update(metadata.idempotency_key, {
          conversationId: metadata.conversation_id,
          textMessageId,
        });
        record = deliveries.getRecord(metadata.idempotency_key);
      }

      const messageId = record.textMessageId ?? (record.imageUrl ? `image:${record.imageUrl}` : null);
      if (!messageId) throw new Error("outbound request contains neither text nor image");
      await deliveries.update(metadata.idempotency_key, {
        conversationId: metadata.conversation_id,
        messageId,
        completed: true,
      });
      return messageId;
    } finally {
      messageOrderGate.release(metadata.conversation_id);
    }
  });
}

async function readBody(request, maxBytes) {
  const declared = Number.parseInt(request.headers["content-length"] ?? "0", 10);
  if (declared > maxBytes) throw new Error("request body is too large");
  const chunks = [];
  let total = 0;
  for await (const chunk of request) {
    total += chunk.length;
    if (total > maxBytes) throw new Error("request body is too large");
    chunks.push(chunk);
  }
  return Buffer.concat(chunks);
}

async function parseOutboundRequest(request) {
  const body = await readBody(request, config.maxImageBytes + 1024 * 1024);
  const contentType = request.headers["content-type"] ?? "";
  if (contentType.startsWith("application/json")) {
    return { metadata: JSON.parse(body.toString("utf8")), image: null };
  }
  if (!contentType.startsWith("multipart/")) {
    throw new Error("request must be JSON or multipart");
  }
  const parsed = new Request("http://steam-adapter/v1/messages", {
    method: "POST",
    headers: { "content-type": contentType },
    body,
  });
  const form = await parsed.formData();
  const metadataRaw = form.get("metadata");
  if (typeof metadataRaw !== "string") throw new Error("multipart request has no metadata");
  const imageField = form.get("image");
  const image = imageField instanceof Blob ? Buffer.from(await imageField.arrayBuffer()) : null;
  return { metadata: JSON.parse(metadataRaw), image };
}

function validateMetadata(metadata) {
  if (!metadata || typeof metadata !== "object") throw new Error("metadata must be an object");
  for (const field of ["idempotency_key", "conversation_id"]) {
    if (typeof metadata[field] !== "string" || !metadata[field].trim()) {
      throw new Error(`${field} must be a non-empty string`);
    }
  }
  for (const field of ["text", "reply_to_message_id"]) {
    if (metadata[field] !== null && metadata[field] !== undefined && typeof metadata[field] !== "string") {
      throw new Error(`${field} must be a string or null`);
    }
  }
}

function json(response, status, payload) {
  const body = JSON.stringify(payload);
  response.writeHead(status, {
    "content-type": "application/json",
    "content-length": Buffer.byteLength(body),
  });
  response.end(body);
}

const server = createServer(async (request, response) => {
  try {
    const url = new URL(request.url ?? "/", "http://steam-adapter");
    if (request.method === "GET" && url.pathname === "/health") {
      const storeOk = spool.healthProbe();
      const deadLetterEvents = spool.deadLetterCount();
      const ok = connected && spoolAlive && storeOk && deadLetterEvents === 0;
      json(response, ok ? 200 : 503, {
        ok,
        connected,
        spool_alive: spoolAlive,
        spool_error: spoolError,
        pending_events: spool.size(),
        dead_letter_events: deadLetterEvents,
        store_ok: storeOk,
        account_id: accountId,
      });
      return;
    }
    if (request.method !== "POST" || url.pathname !== "/v1/messages") {
      json(response, 404, { error: "not found" });
      return;
    }
    if (request.headers.authorization !== `Bearer ${config.internalToken}`) {
      json(response, 401, { error: "invalid internal token" });
      return;
    }
    const { metadata, image } = await parseOutboundRequest(request);
    validateMetadata(metadata);
    const messageId = await sendOutbound(metadata, image);
    json(response, 200, { ok: true, message_id: messageId });
  } catch (error) {
    log("ERROR", "Steam outbound delivery failed", error);
    if (error?.eresult === 84) {
      json(response, 429, {
        error: error instanceof Error ? error.message : "Steam rate limit exceeded",
        retry_after: 30,
      });
    } else {
      json(response, 502, {
        error: error instanceof Error ? error.message : "delivery failed",
      });
    }
  }
});

const spoolTask = deliverPendingEvents().catch((error) => {
  spoolAlive = false;
  spoolError = error instanceof Error ? error.message : String(error);
  log("ERROR", "Steam pending-event spool stopped", error);
  stopForSpoolFailure(error);
});

server.listen(config.port, config.bind, () => {
  log("INFO", `Steam adapter listening on ${config.bind}:${config.port}`);
});
client.logOn({ refreshToken });

async function shutdown() {
  if (shuttingDown) return;
  shuttingDown = true;
  if (personaRefreshTimer) clearInterval(personaRefreshTimer);
  if (reconnectWatchdog) clearTimeout(reconnectWatchdog);
  server.close();
  client.logOff();
  await spoolTask;
  closeSpool();
}
process.on("SIGTERM", () => void shutdown());
process.on("SIGINT", () => void shutdown());
