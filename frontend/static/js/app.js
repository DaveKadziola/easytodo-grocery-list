// Page shell: headers for every HTMX request (and the page's id, window.easytodo.clientId), toasts
// (from HX-Trigger, the flash cookie, or a request that failed without one), menus that close
// themselves, and copy buttons.
(() => {
  "use strict";

  const FLASH_COOKIE = "easytodo_flash";
  const TOAST_MS = 4000; // how long a toast stays

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

  // realtime.js names the tab the same way when it subscribes to the board.
  window.easytodo = { clientId };

  // The message may quote names that other board members chose, so it is only ever set as text.
  function showToast(level, message) {
    const container = document.getElementById("toasts");
    if (!container || !message) return;
    const toast = document.createElement("div");
    toast.className = level === "error" ? "toast toast-error" : "toast";
    toast.textContent = message;
    container.appendChild(toast);
    setTimeout(() => toast.remove(), TOAST_MS);
  }

  // HX-Trigger: {"toast": {"level": "error", "message": "..."}}
  document.addEventListener("toast", (event) => {
    const { level, message } = event.detail || {};
    showToast(level, message);
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
  // by a click or tap outside it, and when the page goes to the background.
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
    openMenus().forEach((menu) => {
      if (!menu.contains(event.target)) menu.open = false;
    });
  });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") openMenus().forEach((menu) => (menu.open = false));
  });

  // <button data-copy="field-id">: copies that field's value (an invite link). The Clipboard API
  // exists only in secure contexts, so a plain-http LAN install falls back to execCommand.
  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-copy]");
    const field = button && document.getElementById(button.dataset.copy);
    if (!field) return;
    field.select();
    let copied;
    try {
      await navigator.clipboard.writeText(field.value);
      copied = true;
    } catch {
      copied = document.execCommand("copy");
    }
    if (copied) showToast("info", "Copied.");
  });
})();
