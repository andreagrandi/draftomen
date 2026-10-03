"use strict";

const RECEIVER_URL = "http://127.0.0.1:47326/moxgate/snapshot";
let queue = Promise.resolve();

async function postSnapshot(snapshot) {
  const result = {
    pick_index: snapshot.pick_index,
    total_picks: snapshot.total_picks,
    timestamp: Date.now(),
  };
  try {
    const response = await fetch(RECEIVER_URL, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Draftomen-Moxgate": "1",
      },
      body: JSON.stringify(snapshot),
    });
    result.status = response.status;
    if (response.ok) {
      result.result = "accepted";
    } else {
      result.result = "rejected";
      try {
        const body = await response.json();
        result.error = typeof body.error === "string" ? body.error : null;
      } catch (error) {
        result.error = null;
      }
    }
  } catch (error) {
    result.result = "unreachable";
    result.status = null;
    result.error = String(error && error.message ? error.message : error);
  }
  await chrome.storage.session.set({ lastPost: result });
  return result;
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || message.type !== "moxgate-snapshot") {
    return false;
  }
  // Chaining keeps snapshots in the order the content script sent them.
  queue = queue
    .then(() => postSnapshot(message.snapshot))
    .then(
      (result) => sendResponse(result),
      () => sendResponse(undefined),
    );
  return true;
});
