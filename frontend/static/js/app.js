// Page shell: headers for every HTMX request (and the tab's id, window.easytodo.clientId), toasts
// from HX-Trigger or the flash cookie, and copy buttons.
(() => {
  "use strict";

  const CLIENT_ID_KEY = "easytodo.clientId";
  const FLASH_COOKIE = "easytodo_flash";
  let pageClientId = null;

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

  // One id per browser tab, so the server can tell this tab's own changes from others'.
  function clientId() {
    try {
      let id = sessionStorage.getItem(CLIENT_ID_KEY);
      if (!id) {
        id = randomId();
        sessionStorage.setItem(CLIENT_ID_KEY, id);
      }
      return id;
    } catch {
      // Storage blocked by the browser: keep one id for this page.
      pageClientId = pageClientId || randomId();
      return pageClientId;
    }
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
  }

  // HX-Trigger: {"toast": {"level": "error", "message": "..."}}
  document.addEventListener("toast", (event) => {
    const { level, message } = event.detail || {};
    showToast(level, message);
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
