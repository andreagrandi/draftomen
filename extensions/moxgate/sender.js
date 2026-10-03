"use strict";

(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  }
  root.DraftomenMoxgate = Object.assign(root.DraftomenMoxgate || {}, api);
})(globalThis, function () {
  function createSender({ post, retryMs, setTimer, clearTimer }) {
    let generation = 0;
    let pendingTimer = null;

    async function attempt(snapshot, current) {
      let outcome;
      try {
        outcome = await post(snapshot);
      } catch (error) {
        // Usually the extension was reloaded; retrying cannot help.
        return;
      }
      if (current !== generation) {
        return;
      }
      if (outcome && outcome.result === "unreachable") {
        pendingTimer = setTimer(() => {
          pendingTimer = null;
          attempt(snapshot, current);
        }, retryMs);
      }
    }

    function send(snapshot) {
      if (pendingTimer !== null) {
        clearTimer(pendingTimer);
        pendingTimer = null;
      }
      generation += 1;
      return attempt(snapshot, generation);
    }

    return { send };
  }

  return { createSender };
});
