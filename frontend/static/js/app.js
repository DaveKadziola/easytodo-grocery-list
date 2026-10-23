// Page shell: headers for every HTMX request (and the page's id, window.easytodo.clientId), toasts
// (from HX-Trigger, the flash cookie, or a request that failed without one; one can offer Undo),
// menus that close themselves, and copy and download buttons. window.easytodo also holds the wait
// before a socket tries again (retryDelay), for realtime.js and notifications.js.
(() => {
  "use strict";

  const FLASH_COOKIE = "easytodo_flash";
  const TOAST_MS = 4000; // how long a toast stays
  const UNDO_TOAST_MS = 5000; // one with an Undo button stays a little longer
  const DOWNLOAD_URL_MS = 40000; // how long a saved file's blob URL lives (see data-download)
  const RETRY_MAX_S = 30; // the longest wait before a socket tries again

  function randomId() {
    if (crypto.randomUUID) return crypto.randomUUID();
    // randomUUID exists only in secure contexts (https or localhost); a plain-http LAN install
    // gets the same RFC 4122 v4 format from getRandomValues, which works everywhere.
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
    return [hex.slice(0, 8), hex.slice(8, 12), hex.slice(12, 16), hex.slice(16, 20), hex.slice(20)].join("-");
  }

  // One id per page load, so the server can tell this page's own changes from others'. Not kept
  // in sessionStorage: a duplicated tab copies that, and the two tabs would then hide each
  // other's changes. The id only has to live as long as the page and its socket.
  const pageClientId = randomId();
  function clientId() {
    return pageClientId;
  }

  document.addEventListener("htmx:configRequest", (event) => {
    const meta = document.querySelector('meta[name="csrf-token"]');
    event.detail.headers["X-CSRF-Token"] = meta ? meta.content : "";
    event.detail.headers["X-Client-Id"] = clientId();
  });

  // 1, 2, 4… seconds, at most 30, each give or take 20 % so that devices do not all come back at
  // the same moment. The sockets (realtime.js for the board, notifications.js for the bell) ask
  // for it only once one has failed: this script runs after theirs (deferred scripts run in
  // order), but before the first of them connects (DOMContentLoaded).
  function retryDelay(failures) {
    const seconds = Math.min(RETRY_MAX_S, 2 ** failures) * (0.8 + Math.random() * 0.4);
    return Math.min(RETRY_MAX_S, seconds) * 1000;
  }

  // realtime.js names the tab the same way when it subscribes to the board.
  window.easytodo = { clientId, retryDelay };

  // The Undo button of a toast: one POST to undo.url, its answer swapped over undo.swap_into. No
  // source element, so a 404 ("resync": the window is over, or the item is gone) comes from the
  // body and reloads the board (realtime.js). Without that element on the page (the board was
  // reloaded meanwhile) the answer is not swapped in: the target would be missing, and htmx then
  // sends nothing at all.
  function sendUndo(undo) {
    const swap = document.querySelector(undo.swap_into)
      ? { target: undo.swap_into, swap: "outerHTML" }
      : { target: "body", swap: "none" };
    htmx.ajax("POST", undo.url, swap).catch(() => {}); // a failed request shows its own error
  }

  // The message may quote names that other board members chose, so it is only ever set as text.
  // With undo ({url, swap_into}) the toast has an Undo button after the message (see sendUndo).
  function showToast(level, message, undo) {
    const container = document.getElementById("toasts");
    if (!container || !message) return;
    const toast = document.createElement("div");
    toast.className = level === "error" ? "toast toast-error" : "toast";
    if (!(undo && undo.url && undo.swap_into)) {
      toast.textContent = message;
      container.appendChild(toast);
      setTimeout(() => toast.remove(), TOAST_MS);
      return;
    }
    toast.className += " flex items-center gap-3";
    const text = document.createElement("span");
    text.textContent = message;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "toast-action";
    button.textContent = "Undo";
    button.addEventListener("click", () => {
      toast.remove();
      sendUndo(undo);
    });
    toast.append(text, button);
    container.appendChild(toast);
    // The deleted item's name held the focus, and the swap that removes it comes after the
    // HX-Trigger that brings this toast: once it has happened, a focus that fell back to the page
    // goes to the button, so that Undo can be reached from the keyboard.
    setTimeout(() => {
      const focused = document.activeElement;
      if (toast.isConnected && (focused === null || focused === document.body)) {
        button.focus({ preventScroll: true });
      }
    }, 0);
    setTimeout(() => toast.remove(), UNDO_TOAST_MS); // no pause while it is hovered or focused
  }

  // HX-Trigger: {"toast": {"level": "error", "message": "...", "undo": {"url": "...", "swap_into": "#..."}}}
  // (undo is optional).
  document.addEventListener("toast", (event) => {
    const { level, message, undo } = event.detail || {};
    showToast(level, message, undo);
  });

  // What the user just did did not happen (and board.js has undone a tick): no answer at all, or
  // an error answer that brings no toast of its own (a proxy's 502, an unexpected 500).
  // Not for a live update's request (realtime.js marks it), which the user did not start: the
  // header's connection status covers that.
  document.addEventListener("htmx:sendError", (event) => {
    if (event.detail.xhr && event.detail.xhr.easytodoBackground) return;
    showToast("error", "Could not reach the server. Try again.");
  });
  document.addEventListener("htmx:responseError", (event) => {
    if (!event.detail.xhr.getResponseHeader("HX-Trigger")) {
      showToast("error", "Something went wrong. Try again.");
    }
  });

  // A redirect after a plain form post cannot carry HX-Trigger, so the server leaves the toast in
  // this cookie (percent-encoded JSON, see easytodo.web.flash). Show it once and delete it.
  function showFlash() {
    const prefix = FLASH_COOKIE + "=";
    const entry = document.cookie.split("; ").find((part) => part.startsWith(prefix));
    if (!entry) return;
    document.cookie = FLASH_COOKIE + "=; Max-Age=0; Path=/; SameSite=Lax";
    try {
      const { level, message } = JSON.parse(decodeURIComponent(entry.slice(prefix.length)));
      showToast(level, message);
    } catch {
      // A malformed cookie is ignored.
    }
  }
  showFlash();

  // Menus (<details> with a <summary>): one open at a time, closed by Esc (back to its summary),
  // by a click or tap outside it, and when the page goes to the background. A click outside that
  // closes a menu does only that: on a link (a board's card on the list is one) it would also
  // follow it. A summary is no link, so a tap on another menu's button still opens that menu.
  // Keyboard activation (detail 0) is never a dismissing tap, so Enter on a link still follows it.
  function openMenus() {
    return Array.from(document.querySelectorAll("details[open]"));
  }
  document.addEventListener(
    "toggle",
    (event) => {
      const opened = event.target;
      if (!(opened instanceof HTMLDetailsElement) || !opened.open) return;
      openMenus().forEach((menu) => {
        if (menu !== opened && !menu.contains(opened)) menu.open = false;
      });
    },
    true, // toggle does not bubble
  );
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    const menu = openMenus().pop();
    if (!menu) return;
    menu.open = false;
    menu.querySelector("summary").focus();
  });
  document.addEventListener("click", (event) => {
    const outside = openMenus().filter((menu) => !menu.contains(event.target));
    outside.forEach((menu) => (menu.open = false));
    if (outside.length && event.detail > 0 && event.target.closest("a[href]")) event.preventDefault();
  });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") openMenus().forEach((menu) => (menu.open = false));
  });

  // <button data-copy="field-id">: copies that field's value (an invite link, the recovery codes).
  // The Clipboard API exists only in secure contexts, so a plain-http LAN install falls back to
  // execCommand, which needs the field selected; the focus then goes back to the button, as the
  // field may be hidden from assistive technology (aria-hidden).
  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-copy]");
    const field = button && document.getElementById(button.dataset.copy);
    if (!field) return;
    let copied;
    try {
      await navigator.clipboard.writeText(field.value);
      copied = true;
    } catch {
      field.select();
      copied = document.execCommand("copy");
      button.focus();
    }
    if (copied) showToast("info", "Copied.");
  });

  // <button data-download="field-id" data-filename="name.txt">: saves that field's value as a
  // UTF-8 text file. No toast: the browser shows the download itself.
  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-download]");
    const field = button && document.getElementById(button.dataset.download);
    if (!field) return;
    const url = URL.createObjectURL(new Blob([field.value], { type: "text/plain;charset=utf-8" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = button.dataset.filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    // Not at once: iOS Safari reads the blob after this handler has returned, and a revoked URL
    // fails there with "WebKitBlobResource error 1".
    setTimeout(() => URL.revokeObjectURL(url), DOWNLOAD_URL_MS);
  });
})();
