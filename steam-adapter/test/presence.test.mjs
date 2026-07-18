import test from "node:test";
import assert from "node:assert/strict";

import { normalizeSteamPresence } from "../src/presence.mjs";

test("normalizes Steam persona states", () => {
  assert.equal(normalizeSteamPresence(0), "offline");
  assert.equal(normalizeSteamPresence(7), "offline");
  assert.equal(normalizeSteamPresence(1), "online");
  assert.equal(normalizeSteamPresence(5), "online");
  assert.equal(normalizeSteamPresence(6), "online");
  assert.equal(normalizeSteamPresence(2), "busy");
  assert.equal(normalizeSteamPresence(3), "idle");
  assert.equal(normalizeSteamPresence(4), "idle");
  assert.equal(normalizeSteamPresence(undefined), null);
  assert.equal(normalizeSteamPresence(99), null);
});
