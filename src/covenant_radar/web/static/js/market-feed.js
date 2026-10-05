"use strict";

/* Live market headlines. The server-rendered list is the no-script baseline;
   this keeps it current without a reload. Headlines are public market news,
   rendered with textContent only, and links must be http(s). */
(() => {
  const root = () => document.querySelector("[data-market-feed]");
  if (!root() || !window.fetch) return;

  const POLL_MS = 30000;
  const WARMING_MS = 5000; // while the first headlines are still being fetched
  const NEW_MS = 10 * 60 * 1000;
  const seen = new Set();
  const arrived = new Map();
  let timer = 0;
  let pending = false;

  const ago = (iso) => {
    const seconds = Math.max((Date.now() - Date.parse(iso)) / 1000, 0);
    if (!Number.isFinite(seconds)) return "";
    if (seconds < 60) return "just now";
    if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
    return `${Math.floor(seconds / 86400)} d ago`;
  };
  const isNew = (id) => Date.now() - (arrived.get(id) || 0) < NEW_MS;
  const safeUrl = (value) => {
    try {
      const url = new URL(String(value || ""));
      return url.protocol === "https:" || url.protocol === "http:" ? url.href : "";
    } catch (_error) {
      return "";
    }
  };
  const feedUrl = (feed) => {
    const url = new URL(feed.dataset.feedUrl || "", window.location.origin);
    return url.origin === window.location.origin ? url : null;
  };

  const remember = (feed) => {
    feed.querySelectorAll("[data-feed-list] [data-id]").forEach((node) => seen.add(node.dataset.id));
  };
  const tick = (feed) => {
    if (!feed) return;
    feed.querySelectorAll("time[datetime]").forEach((node) => { node.textContent = ago(node.dateTime); });
    feed.querySelectorAll(".mkt-feed__item--new").forEach((node) => {
      if (isNew(node.dataset.id)) return;
      node.classList.remove("mkt-feed__item--new");
      node.querySelector(".mkt-feed__new")?.remove();
    });
  };

  const itemNode = (item) => {
    const id = String(item.id);
    const node = document.createElement("li");
    node.className = isNew(id) ? "mkt-feed__item mkt-feed__item--new" : "mkt-feed__item";
    node.dataset.id = id;
    const href = safeUrl(item.url);
    const link = document.createElement(href ? "a" : "span");
    if (href) {
      link.href = href;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
    }
    link.textContent = String(item.title || "").slice(0, 300);
    const meta = document.createElement("small");
    if (isNew(id)) {
      const badge = document.createElement("span");
      badge.className = "mkt-feed__new";
      badge.textContent = "New";
      meta.append(badge, " ");
    }
    const markets = document.createElement("span");
    markets.className = "mkt-feed__markets";
    markets.textContent = String(item.markets || "");
    const time = document.createElement("time");
    time.dateTime = String(item.published_at || "");
    time.textContent = ago(time.dateTime);
    meta.append(markets, ` · ${String(item.publisher || "").slice(0, 80)} · `, time);
    node.append(link, meta);
    return node;
  };

  const render = (feed, data) => {
    const list = feed.querySelector("[data-feed-list]");
    if (!list) return;
    const items = (Array.isArray(data.items) ? data.items : []).filter((item) => item && item.id);
    const fresh = items.filter((item) => !seen.has(String(item.id)));
    fresh.forEach((item) => {
      seen.add(String(item.id));
      arrived.set(String(item.id), Date.now());
    });
    const shown = Array.from(list.children, (node) => node.dataset.id).join(",");
    if (fresh.length || shown !== items.map((item) => String(item.id)).join(",")) {
      list.replaceChildren(...items.map(itemNode));
    }
    list.hidden = items.length === 0;
    const empty = feed.querySelector("[data-feed-empty]");
    if (empty) {
      empty.hidden = items.length > 0;
      if (!items.length && data.fetched_at) empty.textContent = "No headlines in the last 14 days for these markets.";
    }
    const announce = feed.querySelector("[data-feed-announce]");
    if (announce && fresh.length) {
      announce.textContent = `${fresh.length} new headline${fresh.length === 1 ? "" : "s"}`;
    }
  };

  const status = (feed, data) => {
    const node = feed.querySelector("[data-feed-status]");
    if (node) {
      node.textContent = !data.enabled ? "Offline mode: headlines are not fetched"
        : data.fetched_at ? `Updated ${ago(data.fetched_at)}` : "Fetching headlines…";
    }
    const health = document.querySelector("[data-market-health]");
    if (health && data.health && !data.refreshing) {
      const checked = new Date(data.checked_at);
      const time = Number.isNaN(checked.getTime()) ? ""
        : ` · checked ${checked.toISOString().slice(11, 16)} UTC`;
      health.textContent = `${data.health.current}/${data.health.total} sources current${time}`;
    }
  };

  const schedule = (delay = POLL_MS) => {
    window.clearTimeout(timer);
    timer = window.setTimeout(poll, delay);
  };
  async function poll() {
    const feed = root();
    if (!feed) return; // left the page
    if (pending || document.hidden) return schedule();
    const url = feedUrl(feed);
    if (!url) return;
    pending = true;
    let delay = POLL_MS;
    remember(feed);
    try {
      const response = await fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } });
      if (!response.ok) throw new Error("feed unavailable");
      const data = await response.json();
      // HTMX may have redrawn the workspace while the request was in flight.
      const current = root();
      if (current) {
        remember(current);
        render(current, data);
        status(current, data);
      }
      if (data.enabled && !data.fetched_at) delay = WARMING_MS;
    } catch (_error) {
      const node = root()?.querySelector("[data-feed-status]");
      if (node) node.textContent = "Reconnecting…";
    } finally {
      pending = false;
      tick(root());
      schedule(delay);
    }
  }

  remember(root());
  document.body.addEventListener("htmx:afterSwap", () => { const feed = root(); if (feed) { remember(feed); tick(feed); } });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) void poll(); });
  schedule(root().querySelector("[data-feed-list] [data-id]") ? POLL_MS : WARMING_MS);
})();
