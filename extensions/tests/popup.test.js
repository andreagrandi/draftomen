"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const { describeLastPost } = require("../moxgate/popup.js");

const NOW = 1_000_000;

test("a recent unreachable post reads as waiting for Draft Omen", () => {
  const post = { result: "unreachable", pick_index: 0, total_picks: 42, timestamp: NOW - 4000 };
  assert.equal(describeLastPost(post, NOW), "Pick 1 of 42 is waiting for Draft Omen.");
});

test("an old unreachable post reads as not answered", () => {
  const post = { result: "unreachable", pick_index: 0, total_picks: 42, timestamp: NOW - 60000 };
  assert.match(describeLastPost(post, NOW), /^Last snapshot was not answered at /);
});

test("an accepted post keeps its text", () => {
  const post = { result: "accepted", pick_index: 4, total_picks: 42, timestamp: NOW };
  assert.match(describeLastPost(post, NOW), /^Last snapshot accepted: pick 5 of 42 at /);
});

test("a rejected post keeps its text", () => {
  const post = { result: "rejected", status: 422, error: "bad pack", timestamp: NOW };
  assert.match(describeLastPost(post, NOW), /^Last snapshot rejected at .*: bad pack$/);
});

test("a missing post reads as nothing sent", () => {
  assert.equal(describeLastPost(undefined, NOW), "No snapshot sent yet.");
});
