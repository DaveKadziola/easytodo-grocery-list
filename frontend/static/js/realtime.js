// Live board updates (easytodo.realtime): a WebSocket per board page, its state shown in the
// header. The server sends thin events ({"type", "board_id", "category_ids"}) and the page fetches
// what they name through the normal routes, as HTML: one category, or the whole board. Every
// (re)connect reloads the whole board, since events may have been missed while the socket was down.
// An update waits while the user is in the middle of something it would replace: a finger or
// mouse button down, typing in one of its fields, a drag or the item modal (board.js), or one of
// this tab's own requests (whose answer brings the change anyway). It runs once they are done.
// Loaded on the board page after board.js; connects once app.js has run (DOMContentLoaded).
(() => {
  "use strict";

  const BOARD_CONTENT = "#board-content";
  const TEXT_FIELDS = "input:not([type=checkbox]), textarea, select";
  const MAX_DELAY_S = 30;
  const SIGNED_OUT = 4401; // the session ended: sign in again
  const NO_ACCESS = [4403, 4404]; // removed from the board, or the board is gone

  // Updates not run yet: the whole board, or single categories to fetch again or to remove.
  const pending = { board: false, changed: new Set(), deleted: new Set() };
  const pointersDown = new Set();
  let ownRequests = 0; // this tab's htmx requests in flight
  let refreshing = false; // an update's request is in flight: one at a time, so none overtakes another
  let refreshXhr = null; // that request
  let sendingRefresh = false; // set while htmx.ajax sends it (htmx:beforeSend comes synchronously)
  let flushQueued = false;

  const board = () => window.easytodoBoard;
  const categoryElement = (id) => document.getElementById(`category-${id}`);

  // The connection in the header: data-state colours its dot, the word goes in its <span>.
  function setStatus(state, text) {
    const status = document.getElementById("conn-status");
    if (!status) return;
    status.dataset.state = state;
    status.querySelector("span").textContent = text;
  }

  // Whether replacing `element` now would get in the user's way.
  function busy(element) {
    const active = document.activeElement;
    return (
      pointersDown.size > 0 ||
      ownRequests > 0 ||
      board().isBusy() ||
      (active !== null && element.contains(active) && active.matches(TEXT_FIELDS))
    );
  }

  function scheduleFlush() {
    if (flushQueued) return;
    flushQueued = true;
    setTimeout(flush);
  }

  // Runs the pending updates that are not in the user's way.
  function flush() {
    flushQueued = false;
    const content = document.querySelector(BOARD_CONTENT);
    if (refreshing || !content) return;
    for (const id of pending.deleted) {
      const category = categoryElement(id);
      if (category && busy(category)) continue;
      pending.deleted.delete(id);
      pending.changed.delete(id);
      if (category) htmx.swap(category, "", { swapStyle: "delete" });
    }
    if (!pending.board) {
      for (const id of pending.changed) {
        const category = categoryElement(id);
        if (!category) {
          pending.board = true; // a new category: only the whole board has it
          break;
        }
        if (busy(category)) continue;
        pending.changed.delete(id);
        sendRefresh(() =>
          htmx.ajax("GET", `/categories/${id}`, { source: category, target: category, swap: board().reloadSwap }),
        );
        return;
      }
    }
    if (pending.board && !busy(content)) {
      pending.board = false;
      pending.changed.clear();
      pending.deleted.clear();
      sendRefresh(() => board().refresh());
    }
  }

  function sendRefresh(send) {
    sendingRefresh = true;
    let sent;
    try {
      sent = send();
    } finally {
      sendingRefresh = false;
    }
    refreshing = true;
    Promise.resolve(sent)
      .catch(() => {}) // a failed request shows its own error
      .then(() => {
        refreshing = false;
        refreshXhr = null;
        scheduleFlush();
      });
  }

  document.addEventListener("htmx:beforeSend", (event) => {
    const { xhr } = event.detail;
    if (sendingRefresh) {
      refreshXhr = xhr;
      return;
    }
    ownRequests += 1;
    xhr.addEventListener(
      "loadend",
      () => {
        ownRequests -= 1;
        scheduleFlush();
      },
      { once: true },
    );
  });

  // An update sent while the user was idle can answer once they are not: it is not swapped in
  // then, but fetched again when they are done.
  document.addEventListener("htmx:beforeSwap", (event) => {
    const { xhr, target, shouldSwap } = event.detail;
    if (xhr !== refreshXhr || !shouldSwap || !busy(target)) return;
    event.detail.shouldSwap = false;
    if (target.matches(BOARD_CONTENT)) pending.board = true;
    else pending.changed.add(Number(target.dataset.categoryId));
  });

  // The board's name comes with its content; the tab's title follows it.
  document.addEventListener("htmx:afterSwap", () => {
    const content = document.querySelector(BOARD_CONTENT);
    if (content) document.title = `${content.dataset.boardName} · EasyToDo`;
  });

  document.addEventListener("pointerdown", (event) => pointersDown.add(event.pointerId), true);
  for (const type of ["pointerup", "pointercancel"]) {
    document.addEventListener(
      type,
      (event) => {
        pointersDown.delete(event.pointerId);
        scheduleFlush();
      },
      true,
    );
  }
  document.addEventListener("focusout", scheduleFlush); // runs once the focus has moved on
  document.addEventListener("board:idle", scheduleFlush);

  function handle(message) {
    if (message.reload) {
      location.reload(); // the user's own role changed: other controls, the item modal too
      return;
    }
    switch (message.type) {
      case "category.changed":
        message.category_ids.forEach((id) => pending.changed.add(id));
        break;
      case "category.deleted":
        message.category_ids.forEach((id) => pending.deleted.add(id));
        break;
      case "board.deleted":
      case "member.removed":
        location.assign("/boards");
        return;
      default: // categories.reordered, board.updated, resync
        pending.board = true;
    }
    scheduleFlush();
  }

  // 1, 2, 4… seconds, at most 30, each give or take 20 % so that devices do not all come back
  // at the same moment.
  function retryDelay(failures) {
    const seconds = Math.min(MAX_DELAY_S, 2 ** failures) * (0.8 + Math.random() * 0.4);
    return Math.min(MAX_DELAY_S, seconds) * 1000;
  }

  function connectBoard(boardId) {
    let socket = null;
    let failures = 0;
    let retry = null;

    function connect() {
      clearTimeout(retry);
      const scheme = location.protocol === "https:" ? "wss:" : "ws:";
      const clientId = encodeURIComponent(window.easytodo.clientId());
      const ws = new WebSocket(`${scheme}//${location.host}/ws/boards/${boardId}?client_id=${clientId}`);
      socket = ws;
      ws.addEventListener("open", () => {
        failures = 0;
        setStatus("live", "Live");
        pending.board = true; // whatever happened while not connected
        scheduleFlush();
      });
      ws.addEventListener("message", (event) => handle(JSON.parse(event.data)));
      ws.addEventListener("close", (event) => {
        socket = null;
        if (event.code === SIGNED_OUT) {
          location.assign("/login");
        } else if (NO_ACCESS.includes(event.code)) {
          // As after a board.deleted or member.removed message; also when that happened while
          // the socket was down, and the reconnect is refused.
          location.assign("/boards");
        } else {
          setStatus("reconnecting", "Reconnecting…");
          retry = setTimeout(connect, retryDelay(failures));
          failures += 1;
        }
      });
    }

    // Back on the tab, or back online: no waiting for the next retry. A socket that still looks
    // open may be dead (the server's close was lost while the phone slept or had no signal), so
    // the whole board is reloaded over HTTP then, in case its events never arrived.
    function reconnectNow() {
      if (socket === null) {
        connect();
        return;
      }
      pending.board = true;
      scheduleFlush();
    }

    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") reconnectNow();
    });
    window.addEventListener("online", reconnectNow);
    connect();
  }

  document.addEventListener("DOMContentLoaded", () => {
    const status = document.getElementById("conn-status");
    if (status) connectBoard(status.dataset.boardId);
  });
})();
