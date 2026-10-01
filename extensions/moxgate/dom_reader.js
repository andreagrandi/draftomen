"use strict";

(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  }
  root.DraftomenMoxgate = api;
})(globalThis, function () {
  const COUNTER_PATTERN = /^\s*Pick\s*(\d+)\s*\/\s*(\d+)\s*$/i;
  const UUID_PATTERN =
    /\/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.[a-z]+$/i;

  function readPickCounter(root) {
    for (const element of root.querySelectorAll("span, div, p")) {
      const match = COUNTER_PATTERN.exec(element.textContent || "");
      if (match) {
        return { number: Number(match[1]), total: Number(match[2]) };
      }
    }
    return null;
  }

  function scryfallIdFromSource(source) {
    if (!source) {
      return null;
    }
    const path = source.split(/[?#]/)[0];
    const match = UUID_PATTERN.exec(path);
    return match ? match[1].toLowerCase() : null;
  }

  function readCard(element) {
    const image = element.querySelector("img");
    const name = image ? (image.getAttribute("alt") || "").trim() : "";
    return {
      instanceId: element.getAttribute("data-instance-id"),
      scryfallId: image ? scryfallIdFromSource(image.getAttribute("src")) : null,
      name: name || null,
    };
  }

  function readDraftPage(root) {
    const pack = [];
    const pool = [];
    const seen = new Set();
    for (const element of root.querySelectorAll("[data-instance-id]")) {
      const instanceId = element.getAttribute("data-instance-id");
      if (!instanceId || seen.has(instanceId)) {
        continue;
      }
      seen.add(instanceId);
      const inPool = element.closest("[data-card-id]") !== null;
      (inPool ? pool : pack).push(readCard(element));
    }
    return { pick: readPickCounter(root), pack, pool };
  }

  function toSnapshotCards(cards) {
    if (cards.some((card) => !card.name || !card.scryfallId)) {
      return null;
    }
    return cards.map((card) => ({ scryfall_id: card.scryfallId, name: card.name }));
  }

  function buildSnapshot({ pickIndex, totalPicks, page }) {
    const pack = toSnapshotCards(page.pack);
    const pool = toSnapshotCards(page.pool);
    if (pack === null || pool === null) {
      return null;
    }
    return {
      schema_version: 1,
      pick_index: pickIndex,
      total_picks: totalPicks,
      pack,
      pool,
    };
  }

  function toSnapshot({ page, totalPicks }) {
    if (page.pick) {
      const { number, total } = page.pick;
      if (page.pack.length === 0 || page.pool.length !== number - 1) {
        return null;
      }
      return buildSnapshot({ pickIndex: number - 1, totalPicks: total, page });
    }
    if (
      !totalPicks ||
      page.pack.length !== 0 ||
      page.pool.length !== totalPicks
    ) {
      return null;
    }
    return buildSnapshot({ pickIndex: totalPicks, totalPicks, page });
  }

  return { readDraftPage, toSnapshot };
});
