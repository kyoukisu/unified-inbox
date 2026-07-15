import test from "node:test";
import assert from "node:assert/strict";

import { messageIdentity, parseFriendMessage } from "../src/message.mjs";

test("parses Steam text and embedded images", () => {
  const parsed = parseFriendMessage({
    message_bbcode_parsed: [
      "look ",
      {
        tag: "img",
        attrs: { src: "https://images.akamai.steamusercontent.com/ugc/picture.png" },
        content: [],
      },
      " nice",
    ],
  });

  assert.equal(parsed.text, "look  nice");
  assert.deepEqual(parsed.attachments, [
    {
      url: "https://images.akamai.steamusercontent.com/ugc/picture.png",
      filename: "picture.png",
      mime_type: "image/png",
    },
  ]);
});

test("builds stable timestamp and ordinal identity", () => {
  assert.equal(
    messageIdentity({ server_timestamp: new Date("2026-07-15T10:00:00Z"), ordinal: 2 }),
    "1784109600:2",
  );
});
