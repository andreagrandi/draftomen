"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const EXTENSION_DIR = path.join(__dirname, "..", "moxgate");
const manifest = JSON.parse(fs.readFileSync(path.join(EXTENSION_DIR, "manifest.json"), "utf8"));
const SIZES = ["16", "32", "48", "128"];

function pngSize(file) {
  // Width and height are big-endian integers in the IHDR chunk at bytes 16 and 20.
  const header = fs.readFileSync(file).subarray(0, 24);
  return { width: header.readUInt32BE(16), height: header.readUInt32BE(20) };
}

for (const [label, icons] of [["icons", manifest.icons], ["action.default_icon", manifest.action.default_icon]]) {
  test(`${label} lists every icon size with a file of matching dimensions`, () => {
    assert.deepEqual(Object.keys(icons).sort(), [...SIZES].sort());
    for (const size of SIZES) {
      const { width, height } = pngSize(path.join(EXTENSION_DIR, icons[size]));
      assert.equal(width, Number(size));
      assert.equal(height, Number(size));
    }
  });
}
