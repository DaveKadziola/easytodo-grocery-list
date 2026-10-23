// Live board updates (easytodo.realtime): a WebSocket per board page, its state shown in the
// header. The server sends thin events ({"type", "board_id", "category_ids"}, and "item_ids" when
// the details of items changed) and the page fetches what they name through the normal routes, as
// HTML: one category, or the whole board, or the details of the item that is open. Every
// (re)connect reloads the whole board, since events may have been missed while the socket was down.
// An update waits while the user is in the middle of something it would replace: a finger or
// mouse button down, typing in one of its fields (a field with text in it, an IME composition, or
// a key pressed in the last moments; an empty field that only has the cursor does not count, so
// the cursor left in "Add item" after an add holds nothing), an open menu, a drag or the item
// modal (board.js), or one of this tab's own requests (whose answer brings the change anyway). It
// runs once they are done. board.js keeps the cursor and any typed text across the swap.
// The open item's details are another matter: they are in the modal, outside the board, and only
// #item-details is replaced, so they refresh while the modal is open (and the board waits for it
// to close); only a finger down, an own request, a drag of a checklist entry or typing in the
// details themselves holds them. The closing modal tells this tab's own checklist changes
// ("board:changed"): the server does not send a tab its own events.
// Loaded on the board page after board.js; connects once app.js has run (DOMContentLoaded).
(() => {
  "use strict";

  const BOARD_CONTENT = "#board-content";
  const TEXT_FIELDS = "input:not([type=checkbox]):not([type=hidden]), textarea, select";
  const TYPING_PAUSE_MS = 1500; // a key pressed this recently still counts as typing
  const STALE_SOCKET_MS = 25000; // hidden longer than the server's ping interval: a new socket
  const SIGNED_OUT = 4401; // the session ended: sign in again
  const NO_ACCESS = [4403, 4404]; // removed from the board, or the board is gone
  const WRONG_ADDRESS = 4400; // the page is at an address the server does not accept

  // Updates not run yet: the whole board, or single categories to fetch again or to remove, or the
  // details of the open item.
  const pending = { board: false, changed: new Set(), deleted: new Set(), details: false };
  // A board reload due because one of this tab's own changes lost a race (a 404 "resync"): the
  // user is waiting for it, so a focused field does not hold it.
  let boardUrgent = false;
  let reloadDue = false; // the user's role changed: reload the page at the next idle moment
  const pointersDown = new Set();
  const ownRequests = new Set(); // this tab's htmx requests in flight (their XHRs)
  let ownActivity = 0; // counts each start and end of one of them
  let refreshing = false; // an update's request is in flight: one at a time, so none overtakes another
  let refreshXhr = null; // that request
  let refreshActivity = 0; // ownActivity when it was sent
  let answering = null; // the XHR whose answer htmx is handling right now
  let sendingRefresh = false; // set while htmx.ajax sends it (htmx:beforeSend comes synchronously)
  let detailsRefreshing = false; // the same for the open item's details (their own, one at a time)
  let detailsXhr = null;
  let detailsActivity = 0;
  let sendingDetails = false;
  let flushQueued = false;
  let composing = false; // an IME composition is running
  let lastKey = { field: null, at: 0 }; // where and when the user last typed

  const board = () => window.easytodoBoard;
  const categoryElement = (id) => document.getElementById(`category-${id}`);

  // The connection in the header: data-state sets its dot (shape and colour), the word goes in
  // its <span>.
  function setStatus(state, text) {
    const status = document.getElementById("conn-status");
    if (!status) return;
    status.dataset.state = state;
    status.querySelector("span").textContent = text;
  }

  function edited(field) {
    if (field.matches("select")) {
      return Array.from(field.options).some((option) => option.selected !== option.defaultSelected);
    }
    return field.value !== field.defaultValue;
  }

  // Whether the user is typing in a field inside `element`: the focused field has text of their
  // own in it, a composition is running, or they pressed a key in it just now.
  function typingIn(element) {
    const active = document.activeElement;
    if (active === null || !element.contains(active) || !active.matches(TEXT_FIELDS)) return false;
    return (
      composing ||
      edited(active) ||
      (lastKey.field === active && performance.now() - lastKey.at < TYPING_PAUSE_MS)
    );
  }

  // Whether replacing `element` now would get in the user's way.
  function busy(element, { typing = true } = {}) {
    return (
      pointersDown.size > 0 ||
      ownRequests.size > 0 ||
      board().isBusy() ||
      element.matches("details[open]") ||
      element.querySelector("details[open]") !== null ||
      (typing && typingIn(element))
    );
  }

  // Whether a file is chosen in the details' form and not uploaded yet: a new copy of the form
  // would not have it (a browser does not let a page set a file field).
  function fileChosen(details) {
    const input = details.querySelector("#attachment-file");
    return input !== null && input.files.length > 0;
  }

  // Whether replacing the open item's details now would get in the user's way: not the modal (it
  // is open), nor a menu, but a finger down, one of this tab's own requests, a drag (a native one
  // ends the pointer: pointercancel), typing in the details or a file chosen and not sent.
  function detailsBusy() {
    const details = document.getElementById("item-details");
    return (
      pointersDown.size > 0 ||
      ownRequests.size > 0 ||
      board().isDragging() ||
      (details !== null && (typingIn(details) || fileChosen(details)))
    );
  }

  function scheduleFlush() {
    if (flushQueued) return;
    flushQueued = true;
    setTimeout(flush);
  }

  // Fetches the open item's details again when an event named it, and nothing is in the way.
  function flushDetails() {
    if (!pending.details) return;
    if (board().openItemId() === null) {
      pending.details = false; // closed meanwhile: the next opening fetches them anyway
      return;
    }
    if (detailsRefreshing || detailsBusy()) return;
    pending.details = false;
    sendingDetails = true;
    let sent;
    try {
      sent = board().refreshDetails();
    } finally {
      sendingDetails = false;
    }
    detailsRefreshing = true;
    detailsActivity = ownActivity;
    Promise.resolve(sent)
      .catch(() => {}) // a failed request shows its own error
      .then(() => {
        detailsRefreshing = false;
        detailsXhr = null;
        scheduleFlush();
      });
  }

  // Runs the pending updates that are not in the user's way.
  function flush() {
    flushQueued = false;
    flushDetails();
    const content = document.querySelector(BOARD_CONTENT);
    if (refreshing || !content) return;
    if (reloadDue) {
      if (!busy(document.body)) location.reload();
      return;
    }
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
    if (pending.board && !busy(content, { typing: !boardUrgent })) {
      pending.board = false;
      boardUrgent = false;
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
    refreshActivity = ownActivity;
    Promise.resolve(sent)
      .catch(() => {}) // a failed request shows its own error
      .then(() => {
        refreshing = false;
        refreshXhr = null;
        scheduleFlush();
      });
  }

  // Queue a whole-board reload (board.js: a drag or a save that did not go through).
  function queueBoard(urgent) {
    pending.board = true;
    if (urgent) boardUrgent = true;
    scheduleFlush();
  }

  document.addEventListener("htmx:beforeSend", (event) => {
    const { elt, xhr } = event.detail;
    // The bell's requests (notifications.js) are the page's background, not the user's own change
    // to the board: they must not hold updates back or make their answers stale.
    if (elt instanceof Element && elt.closest("#notifications")) return;
    if (sendingRefresh || sendingDetails) {
      if (sendingRefresh) refreshXhr = xhr;
      else detailsXhr = xhr;
      xhr.easytodoBackground = true; // app.js: no "Could not reach the server" for it
      return;
    }
    ownRequests.add(xhr);
    ownActivity += 1;
    xhr.addEventListener(
      "loadend",
      () => {
        ownRequests.delete(xhr);
        ownActivity += 1;
        scheduleFlush();
      },
      { once: true },
    );
  });

  document.addEventListener("htmx:beforeOnLoad", (event) => {
    answering = event.detail.xhr;
  });
  document.addEventListener("htmx:afterOnLoad", () => {
    answering = null;
  });

  // An update's answer is not swapped in when the user got busy meanwhile, or when one of this
  // tab's own requests started or ended while it was on its way (its snapshot may be older than
  // that change, whose own answer is already on the page): it is fetched again later. The same for
  // the open item's details.
  document.addEventListener("htmx:beforeSwap", (event) => {
    const { xhr, target, shouldSwap } = event.detail;
    if (xhr === detailsXhr && shouldSwap) {
      if (ownActivity !== detailsActivity || detailsBusy()) {
        event.detail.shouldSwap = false;
        pending.details = true;
      }
      return;
    }
    if (xhr !== refreshXhr || !shouldSwap) return;
    if (ownActivity === refreshActivity && !busy(target)) return;
    event.detail.shouldSwap = false;
    if (target.matches(BOARD_CONTENT)) pending.board = true;
    else pending.changed.add(Number(target.dataset.categoryId));
  });

  // The board's name comes with its content; the tab's title follows it.
  document.addEventListener("htmx:afterSwap", () => {
    const content = document.querySelector(BOARD_CONTENT);
    if (content) document.title = `${content.dataset.boardName} · EasyToDo`;
  });

  // A 404 answer (the item or category was deleted on another device) triggers "resync": the
  // whole board is reloaded, through the queue like any update, so nothing stale stays on screen.
  // Not for the board reload's own 404 (the board is gone; its board.deleted event says so).
  // When the request came from the open item's details (a checklist entry that is gone), the
  // details are fetched again too: that shows the entry's removal, or says the item is gone. (Not
  // for the details' own 404, which would then ask for them again and again.)
  document.addEventListener("resync", (event) => {
    const content = document.querySelector(BOARD_CONTENT);
    if (!content || event.target === content) return;
    queueBoard(answering === null || (answering !== refreshXhr && answering !== detailsXhr));
    if (answering !== detailsXhr && event.target.closest && event.target.closest("#item-details")) {
      pending.details = true;
    }
  });
  document.addEventListener("board:stale", () => queueBoard(true));
  // The modal closed after a change to the checklist from this tab: the category's tile has the
  // old count, and the hub does not send a tab the events of its own changes.
  document.addEventListener("board:changed", (event) => {
    pending.changed.add(event.detail.categoryId);
    scheduleFlush();
  });

  // Holds that can get stuck when the events that end them are lost (the phone slept, the tab
  // went to the background, the window lost focus mid-touch).
  function clearHolds() {
    pointersDown.clear();
    ownRequests.clear(); // their answers still count as own activity when they come
  }

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
  window.addEventListener("blur", () => {
    pointersDown.clear();
    scheduleFlush();
  });
  document.addEventListener("focusout", scheduleFlush); // runs once the focus has moved on
  document.addEventListener("toggle", scheduleFlush, true); // a menu closed (toggle does not bubble)
  document.addEventListener("input", (event) => {
    lastKey = { field: event.target, at: performance.now() };
    setTimeout(scheduleFlush, TYPING_PAUSE_MS + 50); // the pause that ends it
  });
  document.addEventListener("compositionstart", () => {
    composing = true;
  });
  document.addEventListener("compositionend", () => {
    composing = false;
    scheduleFlush();
  });
  document.addEventListener("board:idle", scheduleFlush);

  function handle(message) {
    if (message.reload) {
      reloadDue = true; // the user's own role changed: other controls, the item modal too
      scheduleFlush();
      return;
    }
    switch (message.type) {
      case "category.changed":
        message.category_ids.forEach((id) => pending.changed.add(id));
        if ((message.item_ids || []).includes(board().openItemId())) pending.details = true;
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

  function connectBoard(boardId) {
    let socket = null;
    let failures = 0;
    let retry = null;
    let hiddenSince = null;

    function connect() {
      clearTimeout(retry);
      const scheme = location.protocol === "https:" ? "wss:" : "ws:";
      const clientId = encodeURIComponent(window.easytodo.clientId());
      const ws = new WebSocket(`${scheme}//${location.host}/ws/boards/${boardId}?client_id=${clientId}`);
      socket = ws;
      // A socket given up for a new one (reconnectNow) is ignored from then on.
      ws.addEventListener("open", () => {
        if (socket !== ws) return;
        failures = 0;
        clearHolds();
        setStatus("live", "Live");
        pending.board = true; // whatever happened while not connected
        pending.details = true;
        scheduleFlush();
      });
      ws.addEventListener("message", (event) => {
        if (socket === ws) handle(JSON.parse(event.data));
      });
      ws.addEventListener("close", (event) => {
        if (socket !== ws) return;
        socket = null;
        if (event.code === SIGNED_OUT) {
          location.assign("/login");
        } else if (NO_ACCESS.includes(event.code)) {
          // As after a board.deleted or member.removed message; also when that happened while
          // the socket was down, and the reconnect is refused.
          location.assign("/boards");
        } else if (event.code === WRONG_ADDRESS) {
          // Opened at an address the server does not know (EASYTODO_PUBLIC_URL and
          // EASYTODO_EXTRA_ORIGINS): localhost instead of the LAN address, say. Live updates and
          // saving cannot work here. Not retried.
          setStatus("offline", "Offline");
          const address = document.getElementById("conn-status").dataset.publicUrl;
          const message = `Live updates and saving do not work at this address. Open EasyToDo at ${address}.`;
          document.dispatchEvent(new CustomEvent("toast", { detail: { level: "error", message } }));
        } else {
          setStatus("reconnecting", "Reconnecting…");
          retry = setTimeout(connect, window.easytodo.retryDelay(failures));
          failures += 1;
        }
      });
    }

    // Back on the tab, or back online: no waiting for the next retry. A socket that still looks
    // open may be dead (the server's close was lost while the phone slept or had no signal): after
    // a long time away it is replaced by a new one (whose open reloads the board); otherwise the
    // whole board is reloaded over HTTP, in case its events never arrived.
    function reconnectNow(longAway) {
      if (socket === null) {
        connect();
      } else if (longAway) {
        const old = socket;
        socket = null;
        old.close();
        connect();
      } else {
        pending.board = true;
        pending.details = true;
        scheduleFlush();
      }
    }

    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "hidden") {
        hiddenSince = performance.now();
        clearHolds();
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
    const status = document.getElementById("conn-status");
    if (status) connectBoard(status.dataset.boardId);
  });
})();
