"use strict";

(function () {
  const DEBOUNCE_MS = 250;
  // Keep in step with WAITING_WINDOW_MS in popup.js.
  const RETRY_MS = 2000;
  let totalPicks = null;
  let lastSent = null;
  let timer = null;

  const sender = globalThis.DraftomenMoxgate.createSender({
    post: async (snapshot) => {
      try {
        return await chrome.runtime.sendMessage({ type: "moxgate-snapshot", snapshot });
      } catch (error) {
        // The extension was reloaded or updated; this page needs a refresh.
        return undefined;
      }
    },
    retryMs: RETRY_MS,
    setTimer: (callback, ms) => setTimeout(callback, ms),
    clearTimer: (handle) => clearTimeout(handle),
  });

  function check() {
    timer = null;
    const page = globalThis.DraftomenMoxgate.readDraftPage(document);
    if (page.pick) {
      totalPicks = page.pick.total;
    }
    const snapshot = globalThis.DraftomenMoxgate.toSnapshot({ page, totalPicks });
    if (snapshot === null) {
      return;
    }
    const serialised = JSON.stringify(snapshot);
    if (serialised === lastSent) {
      return;
    }
    lastSent = serialised;
    sender.send(snapshot);
    if (snapshot.pick_index === snapshot.total_picks) {
      // Later deckbuilding edits must not send anything until a new draft starts.
      totalPicks = null;
    }
  }

  function schedule() {
    if (timer !== null) {
      clearTimeout(timer);
    }
    timer = setTimeout(check, DEBOUNCE_MS);
  }

  new MutationObserver(schedule).observe(document.documentElement, {
    childList: true,
    subtree: true,
    characterData: true,
    attributes: true,
    attributeFilter: ["src", "alt", "data-instance-id"],
  });
  check();
})();
