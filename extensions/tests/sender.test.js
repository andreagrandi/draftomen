"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const { createSender } = require("../moxgate/sender.js");

const RETRY_MS = 2000;

function createHarness(replies) {
  const posted = [];
  const timers = [];
  let now = 0;
  const post = (snapshot) => {
    posted.push(snapshot);
    const reply = typeof replies === "function" ? replies(snapshot, posted.length) : replies;
    return reply instanceof Error ? Promise.reject(reply) : Promise.resolve(reply);
  };
  const sender = createSender({
    post,
    retryMs: RETRY_MS,
    setTimer: (callback, ms) => {
      const timer = { callback, at: now + ms, cancelled: false };
      timers.push(timer);
      return timer;
    },
    clearTimer: (timer) => {
      timer.cancelled = true;
    },
  });
  async function advance(ms) {
    const target = now + ms;
    for (;;) {
      const due = timers
        .filter((timer) => !timer.cancelled && timer.at <= target)
        .sort((a, b) => a.at - b.at)[0];
      if (!due) {
        break;
      }
      due.cancelled = true;
      now = due.at;
      due.callback();
      await flush();
    }
    now = target;
  }
  return { sender, posted, advance };
}

async function flush() {
  for (let i = 0; i < 10; i += 1) {
    await Promise.resolve();
  }
}

test("unreachable snapshot is posted again only after the retry interval and then accepted", async () => {
  let receiverUp = false;
  const { sender, posted, advance } = createHarness(() => ({
    result: receiverUp ? "accepted" : "unreachable",
  }));
  const snapshot = { pick_index: 0, total_picks: 42 };

  sender.send(snapshot);
  await flush();
  assert.equal(posted.length, 1);

  await advance(RETRY_MS - 1);
  assert.equal(posted.length, 1);

  await advance(1);
  assert.equal(posted.length, 2);
  assert.equal(posted[1], snapshot);

  receiverUp = true;
  await advance(RETRY_MS);
  assert.equal(posted.length, 3);

  await advance(RETRY_MS * 5);
  assert.equal(posted.length, 3);
});

test("retries stop after the snapshot is accepted", async () => {
  const { sender, posted, advance } = createHarness({ result: "accepted" });
  sender.send({ pick_index: 0 });
  await flush();
  await advance(RETRY_MS * 5);
  assert.equal(posted.length, 1);
});

test("retries stop after the snapshot is rejected", async () => {
  let calls = 0;
  const { sender, posted, advance } = createHarness(() => {
    calls += 1;
    return { result: calls === 1 ? "unreachable" : "rejected", error: "bad" };
  });
  sender.send({ pick_index: 0 });
  await flush();
  await advance(RETRY_MS);
  assert.equal(posted.length, 2);
  await advance(RETRY_MS * 5);
  assert.equal(posted.length, 2);
});

test("a newer snapshot replaces a pending retry of the older one", async () => {
  const older = { pick_index: 0 };
  const newer = { pick_index: 1 };
  const { sender, posted, advance } = createHarness((snapshot) => ({
    result: snapshot === newer ? "accepted" : "unreachable",
  }));
  sender.send(older);
  await flush();
  await advance(RETRY_MS);
  assert.equal(posted.length, 2);

  sender.send(newer);
  await flush();
  await advance(RETRY_MS * 5);
  assert.deepEqual(posted, [older, older, newer]);
});

test("an unreachable reply for an older snapshot schedules no retry after a newer send", async () => {
  const older = { pick_index: 0 };
  const newer = { pick_index: 1 };
  let resolveOlder;
  const posted = [];
  const timers = [];
  const sender = createSender({
    post: (snapshot) => {
      posted.push(snapshot);
      if (snapshot === older) {
        return new Promise((resolve) => {
          resolveOlder = resolve;
        });
      }
      return Promise.resolve({ result: "accepted" });
    },
    retryMs: RETRY_MS,
    setTimer: (callback, ms) => {
      timers.push({ callback, ms });
      return timers.length;
    },
    clearTimer: () => {},
  });

  sender.send(older);
  sender.send(newer);
  await flush();
  resolveOlder({ result: "unreachable" });
  await flush();

  assert.deepEqual(posted, [older, newer]);
  assert.equal(timers.length, 0);
});

test("a messaging failure stops retries", async () => {
  const { sender, posted, advance } = createHarness(new Error("Extension context invalidated"));
  sender.send({ pick_index: 0 });
  await flush();
  await advance(RETRY_MS * 5);
  assert.equal(posted.length, 1);
});

test("an undefined reply stops retries", async () => {
  const { sender, posted, advance } = createHarness(undefined);
  sender.send({ pick_index: 0 });
  await flush();
  await advance(RETRY_MS * 5);
  assert.equal(posted.length, 1);
});
