"use strict";

const RECEIVER_URL = "http://127.0.0.1:47326/moxgate/snapshot";
const CHECK_TIMEOUT_MS = 2000;
// Three times RETRY_MS in content.js.
const WAITING_WINDOW_MS = 6000;

async function checkReceiver() {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), CHECK_TIMEOUT_MS);
  try {
    // Any HTTP response, even an error status, means the receiver is listening.
    await fetch(RECEIVER_URL, { method: "OPTIONS", signal: controller.signal });
    return true;
  } catch (error) {
    return false;
  } finally {
    clearTimeout(timer);
  }
}

function describeLastPost(post, now) {
  if (!post) {
    return "No snapshot sent yet.";
  }
  const time = new Date(post.timestamp).toLocaleTimeString();
  if (post.result === "accepted") {
    return `Last snapshot accepted: pick ${Math.min(post.pick_index + 1, post.total_picks)} of ${post.total_picks} at ${time}.`;
  }
  if (post.result === "rejected") {
    const reason = post.error || `HTTP ${post.status}`;
    return `Last snapshot rejected at ${time}: ${reason}`;
  }
  if (now - post.timestamp <= WAITING_WINDOW_MS) {
    return `Pick ${Math.min(post.pick_index + 1, post.total_picks)} of ${post.total_picks} is waiting for Draft Omen.`;
  }
  return `Last snapshot was not answered at ${time}.`;
}

async function main() {
  const status = document.getElementById("status");
  const command = document.getElementById("command");
  const lastPost = document.getElementById("last-post");

  const stored = await chrome.storage.session.get("lastPost");
  lastPost.textContent = describeLastPost(stored.lastPost, Date.now());

  if (await checkReceiver()) {
    status.textContent = "Draft Omen is running.";
  } else {
    status.textContent = "Draft Omen is not running.";
    command.hidden = false;
  }
}

if (typeof document !== "undefined") {
  main();
}
if (typeof module === "object" && module.exports) {
  module.exports = { describeLastPost };
}
