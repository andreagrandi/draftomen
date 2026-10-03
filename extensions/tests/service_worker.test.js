"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const SOURCE = fs.readFileSync(path.join(__dirname, "../moxgate/service_worker.js"), "utf8");

function loadWorker(fetchStub) {
  const stored = [];
  let listener = null;
  const chrome = {
    runtime: { onMessage: { addListener: (fn) => { listener = fn; } } },
    storage: { session: { set: async (value) => { stored.push(value); } } },
  };
  vm.runInNewContext(SOURCE, { chrome, fetch: fetchStub, Date, JSON, String, Promise });
  function dispatch(snapshot) {
    return new Promise((resolve) => {
      const returned = listener({ type: "moxgate-snapshot", snapshot }, {}, resolve);
      dispatch.returned.push(returned);
    });
  }
  dispatch.returned = [];
  return { dispatch, stored };
}

const SNAPSHOT = { pick_index: 3, total_picks: 42 };

test("a rejected response is stored with the error and sent back", async () => {
  const { dispatch, stored } = loadWorker(async () => ({
    ok: false,
    status: 422,
    json: async () => ({ error: "pack does not match" }),
  }));
  const response = await dispatch(SNAPSHOT);
  assert.equal(response.result, "rejected");
  assert.equal(response.error, "pack does not match");
  assert.equal(stored.length, 1);
  assert.equal(stored[0].lastPost.result, "rejected");
  assert.equal(stored[0].lastPost.error, "pack does not match");
  assert.equal(stored[0].lastPost.status, 422);
  assert.equal(stored[0].lastPost.pick_index, 3);
});

test("an unreachable receiver is stored and sent back as unreachable", async () => {
  const { dispatch, stored } = loadWorker(async () => {
    throw new TypeError("Failed to fetch");
  });
  const response = await dispatch(SNAPSHOT);
  assert.equal(response.result, "unreachable");
  assert.equal(stored[0].lastPost.result, "unreachable");
  assert.equal(stored[0].lastPost.status, null);
});

test("an accepted response is stored and sent back as accepted", async () => {
  const { dispatch, stored } = loadWorker(async () => ({ ok: true, status: 200 }));
  const response = await dispatch(SNAPSHOT);
  assert.equal(response.result, "accepted");
  assert.equal(stored[0].lastPost.result, "accepted");
});

test("the message listener keeps the response channel open", async () => {
  const { dispatch } = loadWorker(async () => ({ ok: true, status: 200 }));
  await dispatch(SNAPSHOT);
  assert.deepEqual(dispatch.returned, [true]);
});

test("snapshots are posted in the order they were received", async () => {
  const started = [];
  const releases = [];
  const { dispatch } = loadWorker((url, options) => {
    started.push(JSON.parse(options.body).pick_index);
    return new Promise((resolve) => {
      releases.push(() => resolve({ ok: true, status: 200 }));
    });
  });
  const first = dispatch({ pick_index: 1, total_picks: 42 });
  const second = dispatch({ pick_index: 2, total_picks: 42 });
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(started, [1]);
  releases[0]();
  await first;
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(started, [1, 2]);
  releases[1]();
  await second;
});
