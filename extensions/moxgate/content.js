"use strict";

(function () {
  const DEBOUNCE_MS = 250;
  let totalPicks = null;
  let lastSent = null;
  let timer = null;

  function send(snapshot) {
    try {
      const pending = chrome.runtime.sendMessage({ type: "moxgate-snapshot", snapshot });
      if (pending && pending.catch) {
        pending.catch(() => {});
      }
    } catch (error) {
      // The extension was reloaded or updated; this page needs a refresh.
    }
  }

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
    send(snapshot);
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
