"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require("jsdom");

const { readDraftPage, toSnapshot } = require("../moxgate/dom_reader.js");

function loadFixture(name) {
  const html = fs.readFileSync(path.join(__dirname, "fixtures", name), "utf8");
  return new JSDOM(html).window.document;
}

function uuid(number) {
  const digits = String(number).padStart(8, "0");
  return `${digits}-aaaa-4bbb-8ccc-${digits.padStart(12, "0")}`;
}

test("pack fixture returns every pack card and the pick counter", () => {
  const page = readDraftPage(loadFixture("pack.html"));

  assert.deepEqual(page.pick, { number: 1, total: 3 });
  assert.deepEqual(
    page.pack.map((card) => [card.instanceId, card.scryfallId, card.name]),
    [
      ["pc-0-1", uuid(1), "Ashen Lantern"],
      ["pc-0-2", uuid(2), "Brindle Wolf"],
      ["pc-0-3", uuid(3), "Cinder Scout"],
    ],
  );
  assert.deepEqual(page.pool, []);
});

test("the pack and pick header without a slash is not a counter", () => {
  const document = loadFixture("pack.html");
  document.querySelector("span").remove();

  assert.equal(readDraftPage(document).pick, null);
});

test("pool cards stay separate from pack cards", () => {
  const page = readDraftPage(loadFixture("pack_and_pool.html"));

  assert.deepEqual(page.pack.map((card) => card.name), ["Cinder Scout"]);
  assert.deepEqual(page.pool.map((card) => card.name), [
    "Ashen Lantern",
    "Brindle Wolf",
  ]);
});

test("main deck and sideboard cards are all read as pool", () => {
  const page = readDraftPage(loadFixture("deckbuilding.html"));

  assert.equal(page.pick, null);
  assert.deepEqual(page.pack, []);
  assert.deepEqual(page.pool.map((card) => card.name), [
    "Ashen Lantern",
    "Brindle Wolf",
    "Cinder Scout",
  ]);
});

test("double-faced card keeps the full name and the front-face id", () => {
  const page = readDraftPage(loadFixture("double_faced.html"));

  assert.equal(page.pack[0].name, "Dawn Herald // Dusk Raven");
  assert.equal(page.pack[0].scryfallId, "dddddddd-1111-4222-8333-444444444444");
});

test("repeated instance ids are read once", () => {
  const page = readDraftPage(loadFixture("pack_and_pool.html"));

  assert.equal(page.pool.length, 2);
});

test("pack fixture becomes the first snapshot", () => {
  const page = readDraftPage(loadFixture("pack.html"));

  assert.deepEqual(toSnapshot({ page, totalPicks: null }), {
    schema_version: 1,
    pick_index: 0,
    total_picks: 3,
    pack: [
      { scryfall_id: uuid(1), name: "Ashen Lantern" },
      { scryfall_id: uuid(2), name: "Brindle Wolf" },
      { scryfall_id: uuid(3), name: "Cinder Scout" },
    ],
    pool: [],
  });
});

test("pack and pool fixture becomes a mid-draft snapshot", () => {
  const page = readDraftPage(loadFixture("pack_and_pool.html"));

  assert.deepEqual(toSnapshot({ page, totalPicks: 3 }), {
    schema_version: 1,
    pick_index: 2,
    total_picks: 3,
    pack: [{ scryfall_id: uuid(3), name: "Cinder Scout" }],
    pool: [
      { scryfall_id: uuid(1), name: "Ashen Lantern" },
      { scryfall_id: uuid(2), name: "Brindle Wolf" },
    ],
  });
});

test("no-counter fixture gives the final snapshot when the total is known", () => {
  const page = readDraftPage(loadFixture("deckbuilding.html"));
  const snapshot = toSnapshot({ page, totalPicks: 3 });

  assert.equal(snapshot.pick_index, 3);
  assert.equal(snapshot.total_picks, 3);
  assert.deepEqual(snapshot.pack, []);
  assert.equal(snapshot.pool.length, 3);
});

test("no-counter fixture gives nothing without a known total", () => {
  const page = readDraftPage(loadFixture("deckbuilding.html"));

  assert.equal(toSnapshot({ page, totalPicks: null }), null);
});

test("no-counter fixture gives nothing when the pool is not complete", () => {
  const page = readDraftPage(loadFixture("deckbuilding.html"));

  assert.equal(toSnapshot({ page, totalPicks: 4 }), null);
});

test("snapshot is null when the pool length disagrees with the counter", () => {
  const page = readDraftPage(loadFixture("pack_and_pool.html"));
  page.pick = { number: 2, total: 3 };

  assert.equal(toSnapshot({ page, totalPicks: 3 }), null);
});

test("snapshot is null when a card has no scryfall id", () => {
  const document = loadFixture("pack.html");
  document.querySelector("img").setAttribute("src", "https://cards.example.test/x.jpg");
  const page = readDraftPage(document);

  assert.equal(page.pack[0].scryfallId, null);
  assert.equal(toSnapshot({ page, totalPicks: null }), null);
});

test("snapshot is null when a card has no name", () => {
  const document = loadFixture("pack.html");
  document.querySelector("img").setAttribute("alt", " ");
  const page = readDraftPage(document);

  assert.equal(toSnapshot({ page, totalPicks: null }), null);
});
