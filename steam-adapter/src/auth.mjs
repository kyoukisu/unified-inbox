import { mkdir, readFile, rename, rm } from "node:fs/promises";
import { dirname } from "node:path";

import QRCode from "qrcode";
import qrcodeTerminal from "qrcode-terminal";
import { EAuthTokenPlatformType, LoginSession } from "steam-session";
import SteamUser from "steam-user";

import { atomicWritePrivate, readRequiredFile } from "./files.mjs";

const mode = process.env.STEAM_AUTH_MODE ?? "qr";
const tokenFile = process.env.STEAM_REFRESH_TOKEN_FILE ?? "/data/refresh-token";
const qrFile = process.env.STEAM_QR_FILE ?? "/auth/steam-login-qr.png";
const guardCodeFile = process.env.STEAM_GUARD_CODE_FILE ?? "/auth/steam-guard-code";
await mkdir(dirname(tokenFile), { recursive: true, mode: 0o700 });
await mkdir(dirname(qrFile), { recursive: true, mode: 0o700 });

async function waitForGuardCode() {
  console.log(`Write the current Steam Guard code to ${guardCodeFile}`);
  const deadline = Date.now() + 5 * 60 * 1000;
  while (Date.now() < deadline) {
    try {
      const codeContents = await readFile(guardCodeFile, "utf8");
      const code = codeContents.trim();
      if (code) {
        await rm(guardCodeFile, { force: true });
        return code;
      }
    } catch (error) {
      if (error?.code !== "ENOENT") throw error;
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new Error("Timed out waiting for Steam Guard code");
}

async function credentialsLogin() {
  const accountName = await readRequiredFile(
    process.env.STEAM_ACCOUNT_NAME_FILE,
    "Steam account name",
  );
  const password = await readRequiredFile(process.env.STEAM_PASSWORD_FILE, "Steam password");
  const client = new SteamUser({ autoRelogin: false, renewRefreshTokens: true });
  let timeout;

  try {
    await new Promise((resolve, reject) => {
      timeout = setTimeout(() => {
        reject(new Error("Steam credentials login timed out"));
      }, 5 * 60 * 1000);

      client.on("steamGuard", (_domain, callback, lastCodeWrong) => {
        if (lastCodeWrong) console.log("The previous Steam Guard code was rejected.");
        waitForGuardCode().then(callback).catch(reject);
      });
      client.on("refreshToken", async (refreshToken) => {
        try {
          await atomicWritePrivate(tokenFile, refreshToken);
          console.log(`Steam bridge session saved to ${tokenFile}`);
          resolve();
        } catch (error) {
          reject(error);
        }
      });
      client.on("error", reject);
      client.logOn({ accountName, password });
    });
  } finally {
    clearTimeout(timeout);
    client.logOff();
  }
}

async function qrLogin() {
  const session = new LoginSession(EAuthTokenPlatformType.SteamClient, {
    machineId: true,
    machineFriendlyName: "Unified Inbox Bridge",
  });
  session.loginTimeout = 15 * 60 * 1000;

  let currentChallengeUrl = null;
  let currentClientId = null;
  let currentVersion = null;
  let qrWrite = Promise.resolve();

  function updateQr(challengeUrl, reason) {
    if (!challengeUrl || challengeUrl === currentChallengeUrl) return;
    currentChallengeUrl = challengeUrl;
    const match = challengeUrl.match(/^https?:\/\/s\.team\/q\/(\d+)\/(\d+)(?:\?|$)/);
    if (match) {
      currentVersion = match[1];
      currentClientId = match[2];
    }
    qrWrite = qrWrite.then(async () => {
      const temporary = `${qrFile}.tmp-${process.pid}`;
      await QRCode.toFile(temporary, challengeUrl, { width: 640, margin: 2 });
      await rename(temporary, qrFile);
      console.log(`Steam QR ${reason}; image updated at ${qrFile}`);
    });
  }

  session.on("debug", (message, details) => {
    if (message !== "poll response" || !details) return;
    const nextClientId = details.newClientId ? String(details.newClientId) : currentClientId;
    if (details.newChallengeUrl) {
      updateQr(details.newChallengeUrl, "challenge rotated");
    } else if (currentVersion && nextClientId && nextClientId !== currentClientId) {
      updateQr(`https://s.team/q/${currentVersion}/${nextClientId}`, "client ID rotated");
    }
  });
  session.on("remoteInteraction", () => {
    console.log("Steam mobile interaction received; waiting for confirmation.");
  });

  const keepAlive = setInterval(() => {}, 1000);
  const authenticated = new Promise((resolve, reject) => {
    session.on("authenticated", async () => {
      try {
        if (!session.refreshToken) throw new Error("Steam returned no refresh token");
        await atomicWritePrivate(tokenFile, session.refreshToken);
        console.log(`Steam bridge session saved to ${tokenFile}`);
        resolve();
      } catch (error) {
        reject(error);
      }
    });
    session.on("timeout", () => reject(new Error("Steam QR login timed out")));
    session.on("error", reject);
  });

  try {
    const started = await session.startWithQR();
    updateQr(started.qrChallengeUrl, "created");
    await qrWrite;
    console.log("The refresh token will not be printed.");
    qrcodeTerminal.generate(started.qrChallengeUrl, { small: true });
    await authenticated;
  } finally {
    clearInterval(keepAlive);
  }
}

try {
  if (mode === "credentials") {
    await credentialsLogin();
  } else if (mode === "qr") {
    await qrLogin();
  } else {
    throw new Error(`Unsupported STEAM_AUTH_MODE: ${mode}`);
  }
} finally {
  await rm(qrFile, { force: true });
  await rm(guardCodeFile, { force: true });
}
