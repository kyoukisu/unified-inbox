import test from "node:test";
import assert from "node:assert/strict";

import {
  compareMessageOrder,
  messageIdentity,
  parseFriendMessage,
} from "../src/message.mjs";

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

test("ignores malformed Steam image URLs without dropping text", () => {
  const parsed = parseFriendMessage({
    message_bbcode_parsed: [
      "before",
      { tag: "img", attrs: { src: "not a URL" }, content: [] },
      " after",
    ],
  });

  assert.equal(parsed.text, "before after");
  assert.deepEqual(parsed.attachments, []);
});

test("sorts Steam history by timestamp and ordinal", () => {
  const messages = [
    { server_timestamp: new Date("2026-07-15T10:00:01Z"), ordinal: 0 },
    { server_timestamp: new Date("2026-07-15T10:00:00Z"), ordinal: 2 },
    { server_timestamp: new Date("2026-07-15T10:00:00Z"), ordinal: 1 },
  ];

  messages.sort(compareMessageOrder);

  assert.deepEqual(messages.map(messageIdentity), [
    "1784109600:1",
    "1784109600:2",
    "1784109601:0",
  ]);
});

test("builds stable timestamp and ordinal identity", () => {
  assert.equal(
    messageIdentity({ server_timestamp: new Date("2026-07-15T10:00:00Z"), ordinal: 2 }),
    "1784109600:2",
  );
});
