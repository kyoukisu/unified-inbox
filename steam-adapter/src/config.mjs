import { readFile } from "node:fs/promises";

function required(name) {
  const value = process.env[name]?.trim();
  if (!value) {
    throw new Error(`Required environment variable ${name} is missing`);
  }
  return value;
}

async function readSecret(pathName) {
  const path = required(pathName);
  const value = (await readFile(path, "utf8")).trim();
  if (!value) {
    throw new Error(`Secret file configured by ${pathName} is empty`);
  }
  return value;
}

export async function loadConfig() {
  return {
    bind: process.env.ADAPTER_BIND ?? "0.0.0.0",
    port: Number.parseInt(process.env.ADAPTER_PORT ?? "8082", 10),
    coreUrl: required("CORE_URL").replace(/\/$/, ""),
    internalToken: await readSecret("CORE_INTERNAL_TOKEN_FILE"),
    refreshTokenFile: required("STEAM_REFRESH_TOKEN_FILE"),
    deliveriesFile: process.env.STEAM_DELIVERIES_FILE ?? "/data/deliveries.json",
    spoolDatabase: process.env.ADAPTER_DATABASE ?? "/data/steam-adapter.sqlite3",
    maxImageBytes: Number.parseInt(process.env.MAX_IMAGE_BYTES ?? "20971520", 10),
    logLevel: process.env.ADAPTER_LOG_LEVEL ?? "INFO",
  };
}
