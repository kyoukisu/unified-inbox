import { basename } from "node:path";

function isNode(value) {
  return value !== null && typeof value === "object" && typeof value.tag === "string";
}

function inferMimeType(url) {
  const path = new URL(url).pathname.toLowerCase();
  if (path.endsWith(".png")) return "image/png";
  if (path.endsWith(".gif")) return "image/gif";
  if (path.endsWith(".webp")) return "image/webp";
  return "image/jpeg";
}

function imageAttachment(url) {
  const parsed = new URL(url);
  if (parsed.protocol !== "https:") {
    return null;
  }
  return {
    url,
    filename: basename(parsed.pathname) || "steam-image",
    mime_type: inferMimeType(url),
  };
}

export function parseFriendMessage(message) {
  const textParts = [];
  const attachments = [];

  function visit(value) {
    if (typeof value === "string") {
      textParts.push(value);
      return;
    }
    if (!isNode(value)) return;

    if (value.tag === "img" && typeof value.attrs?.src === "string") {
      const attachment = imageAttachment(value.attrs.src);
      if (attachment) attachments.push(attachment);
      return;
    }
    if (value.tag === "sticker" && typeof value.attrs?.type === "string") {
      const attachment = imageAttachment(
        `https://community.cloudflare.steamstatic.com/economy/sticker/${encodeURIComponent(value.attrs.type)}`,
      );
      if (attachment) attachments.push(attachment);
      return;
    }
    if (value.tag === "emoticon" && typeof value.content?.[0] === "string") {
      textParts.push(`:${value.content[0]}:`);
      return;
    }
    if (Array.isArray(value.content)) {
      for (const child of value.content) visit(child);
    }
  }

  const parsed = message.message_bbcode_parsed;
  if (Array.isArray(parsed)) {
    for (const node of parsed) visit(node);
  } else if (typeof message.message_no_bbcode === "string") {
    textParts.push(message.message_no_bbcode);
  }

  const text = textParts.join("").trim();
  return { text: text || null, attachments };
}

export function messageIdentity(message) {
  const rawTimestamp = message.server_timestamp;
  const timestamp =
    rawTimestamp instanceof Date
      ? Math.floor(rawTimestamp.getTime() / 1000)
      : Number(rawTimestamp);
  const ordinal = Number(message.ordinal ?? 0);
  return `${timestamp}:${ordinal}`;
}
