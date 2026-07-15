import { createServer } from "node:http";

import SteamUser from "steam-user";

import { loadConfig } from "./config.mjs";
import { DeliveryStore } from "./deliveries.mjs";
import { atomicWritePrivate, readRequiredFile } from "./files.mjs";
import { messageIdentity, parseFriendMessage } from "./message.mjs";
import { SteamImageUploader } from "./steam-image.mjs";

const config = await loadConfig();
const refreshToken = await readRequiredFile(config.refreshTokenFile, "Steam refresh token");
const deliveries = new DeliveryStore(config.deliveriesFile);
await deliveries.load();

const client = new SteamUser({ autoRelogin: true, renewRefreshTokens: true });
const imageUploader = new SteamImageUploader(client);
let connected = false;
let accountId = null;

function log(level, message, error = null) {
  const line = `${new Date().toISOString()} ${level} ${message}`;
  if (error) console.error(line, error instanceof Error ? error.message : String(error));
  else console.log(line);
}

client.on("loggedOn", () => {
  connected = true;
  accountId = client.steamID?.getSteamID64() ?? null;
  client.setPersona(SteamUser.EPersonaState.Invisible);
  log("INFO", `Steam client connected as ${accountId}`);
});
client.on("disconnected", (result, message) => {
  connected = false;
  log("WARN", `Steam client disconnected (${result}): ${message}`);
});
client.on("error", (error) => log("ERROR", "Steam client error", error));
client.on("refreshToken", async (token) => {
  try {
    await atomicWritePrivate(config.refreshTokenFile, token);
    log("INFO", "Steam refresh token rotated and saved");
  } catch (error) {
    log("ERROR", "Unable to persist rotated Steam refresh token", error);
  }
});

client.chat.on("friendMessage", (message) => {
  void handleFriendMessage(message);
});

async function handleFriendMessage(message) {
  const steamId = message.steamid_friend.toString();
  const identity = messageIdentity(message);
  const { text, attachments } = parseFriendMessage(message);
  if (!text && attachments.length === 0) return;

  let persona = client.users[steamId];
  if (!persona) {
    try {
      const result = await client.getPersonas([steamId]);
      persona = result.personas?.[steamId];
    } catch (error) {
      log("WARN", `Unable to load persona for ${steamId}`, error);
    }
  }
  const displayName = persona?.player_name ?? steamId;
  await deliverToCore({
    platform: "steam",
    event_id: `${steamId}:${identity}`,
    conversation_id: steamId,
    display_name: displayName,
    sender_id: steamId,
    sender_name: displayName,
    message_id: identity,
    text,
    reply_to_message_id: null,
    attachments,
  });
}

async function deliverToCore(payload) {
  let delay = 1000;
  for (let attempt = 0; attempt < 5; attempt += 1) {
    try {
      const response = await fetch(`${config.coreUrl}/v1/events`, {
        method: "POST",
        headers: {
          authorization: `Bearer ${config.internalToken}`,
          "content-type": "application/json",
        },
        body: JSON.stringify(payload),
        signal: AbortSignal.timeout(90_000),
      });
      if (response.ok) return;
      throw new Error(`core returned HTTP ${response.status}: ${(await response.text()).slice(0, 300)}`);
    } catch (error) {
      if (attempt === 4) {
        log("ERROR", `Failed to deliver Steam event ${payload.event_id}`, error);
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, delay));
      delay = Math.min(delay * 2, 15_000);
    }
  }
}

async function sendOutbound(metadata, image) {
  const cached = deliveries.get(metadata.idempotency_key);
  if (cached) return cached;
  if (!connected) throw new Error("Steam client is not connected");

  let imageUrl = null;
  if (image) {
    imageUrl = await imageUploader.sendImageToUser(metadata.conversation_id, image);
  }

  let messageId = imageUrl ? `image:${imageUrl}` : null;
  if (metadata.text) {
    const sent = await client.chat.sendFriendMessage(metadata.conversation_id, metadata.text, {
      containsBbCode: false,
    });
    messageId = messageIdentity(sent);
  }
  if (!messageId) throw new Error("outbound request contains neither text nor image");
  await deliveries.set(metadata.idempotency_key, messageId);
  return messageId;
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
      json(response, 200, { ok: connected, connected, account_id: accountId });
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
    json(response, 502, { error: error instanceof Error ? error.message : "delivery failed" });
  }
});

server.listen(config.port, config.bind, () => {
  log("INFO", `Steam adapter listening on ${config.bind}:${config.port}`);
});
client.logOn({ refreshToken });

async function shutdown() {
  server.close();
  client.logOff();
}
process.on("SIGTERM", () => void shutdown());
process.on("SIGINT", () => void shutdown());
