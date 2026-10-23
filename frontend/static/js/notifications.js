// The bell in the header (easytodo.notifications): its badge, the number of unread notifications,
// and its panel, the latest ones, on every page of a signed-in user. The header holds an empty badge
// that loads itself (hx-get, so rendering a page asks the database nothing for the bell) and the
// panel's heading; this script keeps both current over a WebSocket of its own, /ws/user, which only
// ever says {"type": "notifications.changed"}: the page then asks the normal routes for the HTML it
// shows. The panel is fetched when the bell opens, and opening it reads everything (the badge goes,
// and the user's other devices are told); a notification that arrives while it is open is shown at
// once and read with it. The bell's requests are the page's background: they are not "own
// requests" that hold back board updates (realtime.js), and a failure of the network is silent
// (no toast; #notifications[data-live] says whether the socket is open).
// Loaded after htmx, before the page's own scripts; connects once app.js has run (DOMContentLoaded).
(() => {
  "use strict";

  const MAX_DELAY_S = 30;
  const STALE_SOCKET_MS = 25000; // hidden longer than the server's ping interval: a new socket
  const SIGNED_OUT = 4401; // the session ended
  const WRONG_ADDRESS = 4400; // the page is at an address the server does not accept

  const bell = () => document.getElementById("notifications");
  const panelOpen = () => bell().open;
  const inBell = (element) => element instanceof Element && element.closest("#notifications") !== null;

  // The bell's requests come from the bell itself (never from an element that a swap replaces:
  // htmx drops what it queued for a source that is gone). They are marked as background (app.js
  // shows no toast for them) and a failed one is remembered, to tell ask() apart from a success.
  let failed = false;
  document.addEventListener("htmx:beforeSend", (event) => {
    if (inBell(event.detail.elt)) event.detail.xhr.easytodoBackground = true; // app.js: no toast
  });
  for (const type of ["htmx:responseError", "htmx:sendError"]) {
    document.addEventListener(type, (event) => {
      if (inBell(event.detail.elt)) failed = true;
    });
  }

  // Whether the request went through and its answer is on the page.
  async function ask(verb, path, target) {
    failed = false;
    try {
      await htmx.ajax(verb, path, { source: bell(), target, swap: "outerHTML" });
    } catch {
      return false; // the network, or the page is going away
    }
    return !failed;
  }

  const refreshBadge = () => ask("GET", "/notifications/badge", "#notifications-badge");

  // Fetches the panel and then reads everything in it, one at a time. A change that comes while
  // this runs is fetched afterwards, so nothing arrives unseen between the fetch and the read.
  let showing = false;
  let showAgain = false;
  async function showPanel() {
    if (showing) {
      showAgain = true;
      return;
    }
    showing = true;
    try {
      do {
        showAgain = false;
        const panel = document.getElementById("notifications-panel");
        const hadFocus = panel !== null && panel.contains(document.activeElement);
        const shown = await ask("GET", "/notifications/panel", "#notifications-panel");
        // The swap took the focus from a link in the old panel: it goes back to the bell.
        if (hadFocus && document.activeElement === document.body) bell().querySelector("summary").focus();
        // Closed meanwhile, or the panel did not come: what was never shown is not read.
        if (shown && panelOpen()) await ask("POST", "/notifications/read", "#notifications-badge");
      } while (showAgain);
    } finally {
      showing = false;
    }
  }

  // Something may have changed (a message, the socket opened again, the page is back): show it.
  function update() {
    if (panelOpen()) showPanel();
    else refreshBadge();
  }

  // 1, 2, 4… seconds, at most 30, each give or take 20 % so that devices do not all come back at
  // the same moment (as realtime.js does for the board).
  function retryDelay(failures) {
    const seconds = Math.min(MAX_DELAY_S, 2 ** failures) * (0.8 + Math.random() * 0.4);
    return Math.min(MAX_DELAY_S, seconds) * 1000;
  }

  function connectUser() {
    let socket = null;
    let failures = 0;
    let retry = null;
    let hiddenSince = null;

    function setLive(live) {
      if (live) bell().dataset.live = "";
      else delete bell().dataset.live;
    }

    function connect() {
      clearTimeout(retry);
      const scheme = location.protocol === "https:" ? "wss:" : "ws:";
      const clientId = encodeURIComponent(window.easytodo.clientId());
      const ws = new WebSocket(`${scheme}//${location.host}/ws/user?client_id=${clientId}`);
      socket = ws;
      // A socket given up for a new one (reconnectNow) is ignored from then on.
      ws.addEventListener("open", () => {
        if (socket !== ws) return;
        failures = 0;
        setLive(true);
        update(); // whatever happened while not connected, and since the page was rendered
      });
      ws.addEventListener("message", (event) => {
        if (socket === ws && JSON.parse(event.data).type === "notifications.changed") update();
      });
      ws.addEventListener("close", (event) => {
        if (socket !== ws) return;
        socket = null;
        setLive(false);
        // Signed out, or at an address the server does not accept: another try would be refused
        // again (a signed-out page finds out with its next request, the board page at once).
        if (event.code === SIGNED_OUT || event.code === WRONG_ADDRESS) return;
        retry = setTimeout(connect, retryDelay(failures));
        failures += 1;
      });
    }

    // Back on the tab, or back online: no waiting for the next retry. A socket that still looks
    // open may be dead (the server's close was lost while the phone slept or had no signal): after
    // a long time away it is replaced by a new one (whose open asks again); otherwise the badge is
    // fetched again over HTTP, in case its messages never arrived.
    function reconnectNow(longAway) {
      if (socket === null) {
        connect();
      } else if (longAway) {
        const old = socket;
        socket = null;
        setLive(false);
        old.close();
        connect();
      } else {
        update();
      }
    }

    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "hidden") {
        hiddenSince = performance.now();
        return;
      }
      const away = hiddenSince === null ? 0 : performance.now() - hiddenSince;
      hiddenSince = null;
      reconnectNow(away > STALE_SOCKET_MS);
    });
    window.addEventListener("online", () => reconnectNow(false));
    connect();
  }

  document.addEventListener("DOMContentLoaded", () => {
    const element = bell();
    if (!element) return;
    // toggle does not bubble; it fires after the menu opened or closed (app.js closes the others).
    element.addEventListener("toggle", () => {
      if (element.open) showPanel();
    });
    connectUser();
  });
})();
