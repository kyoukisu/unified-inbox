import { createHash } from "node:crypto";

import { imageSize } from "image-size";

function cookieHeader(cookies) {
  return cookies.map((cookie) => cookie.split(";", 1)[0]).join("; ");
}

async function jsonResponse(response, operation) {
  let body;
  try {
    body = await response.json();
  } catch (error) {
    throw new Error(`${operation} returned invalid JSON`, { cause: error });
  }
  if (!response.ok) {
    throw new Error(`${operation} returned HTTP ${response.status}`);
  }
  return body;
}

export class SteamImageUploader {
  constructor(client) {
    this.client = client;
    this.webSession = null;
    this.waiters = [];
    client.on("webSession", (sessionId, cookies) => {
      this.webSession = {
        sessionId,
        cookies: cookieHeader(cookies),
        createdAt: Date.now(),
      };
      for (const waiter of this.waiters.splice(0)) waiter.resolve(this.webSession);
    });
  }

  async sendImageToUser(steamId, image, { spoiler = false } = {}) {
    const session = await this.#getWebSession();
    try {
      return await this.#upload(session, steamId, image, spoiler);
    } catch (error) {
      this.webSession = null;
      const refreshed = await this.#getWebSession();
      return await this.#upload(refreshed, steamId, image, spoiler);
    }
  }

  async #getWebSession() {
    if (this.webSession && Date.now() - this.webSession.createdAt < 10 * 60 * 1000) {
      return this.webSession;
    }

    const promise = new Promise((resolve, reject) => {
      const waiter = {
        resolve: (session) => {
          clearTimeout(timer);
          resolve(session);
        },
      };
      const timer = setTimeout(() => {
        this.waiters = this.waiters.filter((candidate) => candidate !== waiter);
        reject(new Error("Timed out waiting for Steam web session"));
      }, 30_000);
      this.waiters.push(waiter);
    });
    this.client.webLogOn();
    return await promise;
  }

  async #upload(session, steamId, image, spoiler) {
    const details = imageSize(image);
    if (!details.width || !details.height || !details.type) {
      throw new Error("Unable to determine Steam image dimensions or type");
    }
    const type = details.type === "jpg" ? "jpeg" : details.type;
    const mimeType = `image/${type}`;
    const hash = createHash("sha1").update(image).digest("hex");
    const filename = `${Date.now()}_image.${details.type}`;

    const beginForm = new FormData();
    beginForm.append("sessionid", session.sessionId);
    beginForm.append("l", "english");
    beginForm.append("file_size", String(image.length));
    beginForm.append("file_name", filename);
    beginForm.append("file_sha", hash);
    beginForm.append("file_image_width", String(details.width));
    beginForm.append("file_image_height", String(details.height));
    beginForm.append("file_type", mimeType);

    const begin = await jsonResponse(
      await fetch("https://steamcommunity.com/chat/beginfileupload/?l=english", {
        method: "POST",
        headers: {
          cookie: session.cookies,
          referer: "https://steamcommunity.com/chat/",
        },
        body: beginForm,
        signal: AbortSignal.timeout(30_000),
      }),
      "Steam beginfileupload",
    );
    if (begin.success !== 1 || !begin.result?.ugcid || !begin.result?.url_host) {
      throw new Error(`Steam beginfileupload failed with result ${begin.success}`);
    }

    const uploadUrl = `${begin.result.use_https ? "https" : "http"}://${begin.result.url_host}${begin.result.url_path}`;
    const uploadHeaders = Object.fromEntries(
      (begin.result.request_headers ?? []).map((header) => [
        String(header.name).toLowerCase(),
        String(header.value),
      ]),
    );
    const upload = await fetch(uploadUrl, {
      method: "PUT",
      headers: uploadHeaders,
      body: image,
      signal: AbortSignal.timeout(60_000),
    });
    if (!upload.ok) {
      throw new Error(`Steam image upload returned HTTP ${upload.status}`);
    }

    const commitForm = new FormData();
    const fields = {
      sessionid: session.sessionId,
      l: "english",
      file_name: filename,
      file_sha: hash,
      success: "1",
      ugcid: String(begin.result.ugcid),
      file_type: mimeType,
      file_image_width: String(details.width),
      file_image_height: String(details.height),
      timestamp: String(begin.timestamp),
      hmac: String(begin.hmac),
      friend_steamid: String(steamId),
      spoiler: spoiler ? "1" : "0",
    };
    for (const [name, value] of Object.entries(fields)) commitForm.append(name, value);

    const committed = await jsonResponse(
      await fetch("https://steamcommunity.com/chat/commitfileupload/", {
        method: "POST",
        headers: {
          cookie: session.cookies,
          referer: "https://steamcommunity.com/chat/",
        },
        body: commitForm,
        signal: AbortSignal.timeout(30_000),
      }),
      "Steam commitfileupload",
    );
    const url = committed.result?.details?.url;
    if (committed.success !== 1 || committed.result?.success !== 1 || !url) {
      throw new Error(`Steam commitfileupload failed with result ${committed.success}`);
    }
    return String(url);
  }
}
