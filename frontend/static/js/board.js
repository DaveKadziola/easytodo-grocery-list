// Board page: drag and drop (SortableJS) and Move up / Move down for categories, the item modal
// of those who may edit and the read-only dialog of those who may not (Alpine components
// "itemModal" and "itemViewer", both with the open item's details fetched into their slot, and in
// them the checklist: ticks, and drag and drop or ArrowUp / ArrowDown for its entries), the dialog
// that moves a category to another board (the component "moveCategory"), ticks that
// show at once, fields that are ready for the next entry and keep what the user typed when their
// category or the details are swapped, folding categories away (per device), the bottom bar's "New
// category", and a board reload when a change did not go through.
// Loaded before Alpine (see base.html), so the components are registered on alpine:init. They read
// items only from data-* attributes, which the templates escape; no page data is ever written into
// a script.
// For realtime.js: window.easytodoBoard (the board reload and its swap style, whether a drag or
// the modal is in progress, the open item and a fetch of its details), a "board:idle" event on
// the document when that ends, a "board:stale" event when the page no longer shows what the
// server has (it then queues a reload), and a "board:changed" event ({categoryId}) when the modal
// closes after a change to the checklist (the category's tile shows its count). The category menu's
// "Move to board…" opens the dialog with a "move-category" event on the window ({id, label}).
(() => {
  "use strict";

  const BOARD_CONTENT = "#board-content";
  const TICK = 'input[type="checkbox"][hx-post$="/toggle"]';
  // An entry of the checklist in the item's details: ticked like an item, but nothing is struck.
  const CHECK = 'input[type="checkbox"][hx-post$="/check"]';
  const TICKS = `${TICK}, ${CHECK}`;
  const FOCUSABLE = "a[href], input, textarea, select, button";
  const TEXT_FIELDS = "input[id]:not([type=checkbox]):not([type=hidden]):not([type=file]), textarea[id]";
  // Requests whose failure leaves the page out of step: Sortable has already moved the element.
  const DRAG_REQUEST = /\/items\/\d+\/move$|\/categories\/order$/;
  // The answer to a fetch of an item's details (the path of its request).
  const DETAILS_PATH = /^\/items\/(\d+)\/details$/;
  // The new order of the open item's checklist: if it did not go through, the list is put right.
  const CHECKLIST_ORDER = /\/items\/\d+\/checklist\/order$/;
  const COLLAPSED_KEY = "easytodo.collapsedCategories";
  // A reload that the user did not ask for just now: htmx sets the new content up at once (no
  // settle delay), so a form in it never submits as a plain page request before htmx has it.
  const RELOAD_SWAP = "outerHTML settle:0ms";
  let dragging = false; // a Sortable drag is running
  let editing = false; // the item modal or dialog, or the dialog that moves a category, is open
  let openId = null; // the id (a number) of the item that is open in it
  let detailsChanged = false; // the open item's checklist was changed from this tab
  let newCategoryShown = false; // on a phone the bottom bar's button showed the "New category" form
  let moveOpener = null; // the menu's button ("summary") that opened the move dialog, to get the focus back

  function boardContent() {
    return document.querySelector(BOARD_CONTENT);
  }

  function itemElement(itemId) {
    return document.getElementById(`item-${itemId}`);
  }

  // The element itself (if it matches) and its matching descendants.
  function matching(root, selector) {
    const found = Array.from(root.querySelectorAll(selector));
    if (root.matches && root.matches(selector)) found.unshift(root);
    return found;
  }

  // The board content comes back as a whole. The request is made from #board-content itself, so a
  // "resync" from its own answer (the board is gone) does not start another one. Returns htmx's
  // promise of the request.
  function refreshBoard() {
    const board = boardContent();
    if (board) {
      return htmx.ajax("GET", `/boards/${board.dataset.boardId}/content`, {
        source: board,
        target: board,
        swap: RELOAD_SWAP,
      });
    }
  }

  // The page no longer shows what the server has: realtime.js reloads the board when it can.
  function stale() {
    document.dispatchEvent(new Event("board:stale"));
  }

  function dragStarted() {
    dragging = true;
  }

  // Live updates held back during the drag (or the modal) can run now; a drop's own request,
  // sent just before, is still waited for (realtime.js).
  function becameIdle() {
    document.dispatchEvent(new Event("board:idle"));
  }

  // `source` must be on the page and not inside a form (its fields would be sent along).
  function moveItem(itemId, categoryId, source) {
    return htmx
      .ajax("POST", `/items/${itemId}/move`, {
        source,
        target: BOARD_CONTENT,
        values: { category_id: categoryId },
      })
      .catch(stale);
  }

  function categoryIds() {
    return Array.from(document.querySelectorAll("#categories > [data-category-id]"), (el) => el.dataset.categoryId);
  }

  function saveCategoryOrder(source) {
    const boardId = boardContent().dataset.boardId;
    return htmx
      .ajax("PUT", `/boards/${boardId}/categories/order`, { source, swap: "none", values: { ids: categoryIds() } })
      .catch(stale);
  }

  function checklistIds(list) {
    return Array.from(list.querySelectorAll("[data-entry-id]"), (el) => el.dataset.entryId);
  }

  // The new order of the open item's checklist goes to the server one request at a time, and the
  // last order the user made is the one that stays. A second move while the first is still on
  // its way is noted here, not sent: its request would be queued by htmx on the list, which the
  // first answer replaces, and htmx drops what it queued for an element that is gone. When the
  // first has been answered the noted order is sent, and the first answer is not swapped in
  // meanwhile (it would show the order from before the second move).
  let savingOrder = false; // a request with the order is on its way
  let nextOrder = null; // {itemId, ids}: the order made since, to be sent when it has been answered

  // The answer is the item's details again, in the new order. A request that fails is put right
  // by failed() below (it hears the same error), so there is nothing to catch here.
  function saveChecklistOrder(list) {
    const ids = checklistIds(list);
    if (savingOrder) {
      nextOrder = { itemId: openId, ids };
      return Promise.resolve();
    }
    savingOrder = true;
    return htmx
      .ajax("PUT", `/items/${openId}/checklist/order`, { source: list, target: "#item-details", values: { ids } })
      .catch(() => {})
      .then(sendNextOrder);
  }

  function sendNextOrder() {
    savingOrder = false;
    const next = nextOrder;
    nextOrder = null;
    if (next === null) return undefined;
    const list = document.querySelector("#item-details [data-sortable-checklist]");
    if (list && next.itemId === openId) return saveChecklistOrder(list);
    // The modal was closed (or another item opened) since: the order still has to be saved, and
    // nothing on the page shows its answer.
    savingOrder = true;
    return htmx
      .ajax("PUT", `/items/${next.itemId}/checklist/order`, {
        source: document.body,
        swap: "none",
        values: { ids: next.ids },
      })
      .catch(() => {})
      .then(sendNextOrder);
  }

  function initSortable(root) {
    matching(root, "[data-sortable-items]").forEach((list) => {
      if (Sortable.get(list)) return;
      Sortable.create(list, {
        group: "items", // an item can be dropped into any category's list
        sort: false, // within a category the server's order stays (open first, then by name)
        draggable: "[data-item-id]",
        delay: 150, // on a touch screen a drag needs a long press, so a swipe still scrolls
        delayOnTouchOnly: true,
        // Touch events rather than pointer events: a swipe that scrolled ends in pointercancel,
        // which Sortable 1.15 does not listen for, and it would then ignore the next long press.
        supportPointer: false,
        onStart: dragStarted,
        onEnd(event) {
          dragging = false;
          // Sortable leaves style="" behind, which a strict CSP will not let htmx copy over.
          if (event.item.getAttribute("style") === "") event.item.removeAttribute("style");
          if (event.from !== event.to) {
            const category = event.to.closest("[data-category-id]");
            moveItem(event.item.dataset.itemId, category.dataset.categoryId, event.item);
          }
          becameIdle();
        },
      });
    });
    matching(root, "[data-sortable-checklist]").forEach((list) => {
      if (Sortable.get(list)) return;
      Sortable.create(list, {
        group: "checklist",
        handle: ".checklist-handle", // touch-action: none, so a finger on it drags, not scrolls
        draggable: "[data-entry-id]",
        supportPointer: false, // as for categories: touch events, which end properly on every phone
        onStart: dragStarted,
        onEnd(event) {
          dragging = false;
          if (event.item.getAttribute("style") === "") event.item.removeAttribute("style");
          if (event.oldIndex !== event.newIndex) saveChecklistOrder(list);
          becameIdle();
        },
      });
    });
    matching(root, "[data-sortable-categories]").forEach((container) => {
      if (Sortable.get(container)) return;
      Sortable.create(container, {
        handle: ".category-handle", // touch-action: none, so a finger on it drags, not scrolls
        draggable: "[data-category-id]",
        supportPointer: false, // as for items: touch events, which end properly on every phone
        onStart: dragStarted,
        onEnd(event) {
          dragging = false;
          if (event.item.getAttribute("style") === "") event.item.removeAttribute("style");
          if (event.oldIndex !== event.newIndex) saveCategoryOrder(container);
          becameIdle();
        },
      });
    });
  }

  // Categories folded away on this device (their ids), kept in localStorage when it works.
  function collapsedIds() {
    try {
      const ids = JSON.parse(localStorage.getItem(COLLAPSED_KEY) || "[]");
      return new Set(Array.isArray(ids) ? ids.map(String) : []);
    } catch {
      return new Set();
    }
  }

  function saveCollapsed(ids) {
    try {
      localStorage.setItem(COLLAPSED_KEY, JSON.stringify(Array.from(ids)));
    } catch {
      // Storage blocked: the category stays folded until the page is reloaded.
    }
  }

  function showCollapsed(section, collapsed) {
    const button = section.querySelector("[data-collapse]");
    const body = section.querySelector("[data-category-body]");
    if (!button || !body) return;
    body.hidden = collapsed;
    button.setAttribute("aria-expanded", String(!collapsed));
    button.setAttribute("aria-label", collapsed ? "Show items" : "Hide items");
  }

  let collapsedNow = null; // read once per page, then kept in step with the clicks
  function applyCollapsed(root) {
    if (collapsedNow === null) collapsedNow = collapsedIds();
    matching(root, "section[data-category-id]").forEach((section) => {
      showCollapsed(section, collapsedNow.has(section.dataset.categoryId));
    });
  }

  function newCategoryForm() {
    const field = document.getElementById("new-category-name");
    return field && field.form;
  }

  // Below 640 px the "New category" form is hidden (the bottom bar has the button); once the
  // button showed it, it stays shown, also after the board is reloaded.
  function showNewCategoryForm() {
    const form = newCategoryForm();
    if (form && form.classList.contains("hidden")) form.classList.replace("hidden", "flex");
  }

  function setUp(root) {
    initSortable(root);
    applyCollapsed(root);
    if (newCategoryShown) showNewCategoryForm();
  }

  document.addEventListener("DOMContentLoaded", () => setUp(document));

  // What the user typed into the fields of a category (or the board) that is about to be
  // replaced, so the new copy gets it back: text not submitted yet survives a live update or
  // another request's answer, and so does the cursor. A form's own submit answer leaves out what
  // it sent (that is on the page now) but keeps anything typed since.
  const sentValues = new WeakMap(); // form -> {field id: value} as sent
  let kept = null; // {fields: [{id, value, start, end}], focused: id}

  document.addEventListener("htmx:configRequest", (event) => {
    const { elt, parameters } = event.detail;
    if (elt.matches(TICKS)) parameters.done = String(elt.checked); // the state its user wants
    if (elt instanceof HTMLFormElement) {
      const sent = {};
      for (const field of elt.querySelectorAll(TEXT_FIELDS)) sent[field.id] = field.value;
      sentValues.set(elt, sent);
    }
  });

  document.addEventListener("htmx:beforeSwap", (event) => {
    kept = null;
    const { target, requestConfig, xhr } = event.detail;
    // The details of an item that is not the open one any more (another opened meanwhile, or none)
    // are not swapped in. (An answer for the open item whose placeholder was replaced since, by a
    // reopening, aims at an element that is gone: htmx does nothing with that, and its event never
    // reaches the document.)
    const details = requestConfig && DETAILS_PATH.exec(requestConfig.path);
    if (details && Number(details[1]) !== openId) {
      event.detail.shouldSwap = false;
      return;
    }
    // The answer to an order that a newer one (made since, or being made) has overtaken would put
    // the list back as it was: the page keeps the user's list, and the newer order is saved next.
    if (requestConfig && CHECKLIST_ORDER.test(requestConfig.path) && (nextOrder !== null || dragging)) {
      event.detail.shouldSwap = false;
      return;
    }
    if (!(target instanceof Element)) return;
    const form = requestConfig && requestConfig.elt;
    const sent = (xhr.status < 400 && form instanceof HTMLFormElement && sentValues.get(form)) || {};
    const fields = matching(target, TEXT_FIELDS)
      .filter((field) => field.value !== field.defaultValue && field.value !== sent[field.id])
      .map((field) => ({ id: field.id, value: field.value, start: field.selectionStart, end: field.selectionEnd }));
    const active = document.activeElement;
    const focused = active && active.id && target.contains(active) ? active.id : null;
    if (fields.length || focused) kept = { fields, focused };
  });

  function restoreKept() {
    if (kept === null) return;
    const { fields, focused } = kept;
    kept = null;
    for (const { id, value, start, end } of fields) {
      const field = document.getElementById(id);
      if (!field || field.value !== field.defaultValue) continue;
      field.value = value;
      if (id === focused && field.setSelectionRange && start !== null) {
        try {
          field.setSelectionRange(start, end);
        } catch {
          // Not a text input (email, number): the cursor goes to the end.
        }
      }
    }
    const active = document.activeElement;
    if (focused && (active === null || active === document.body)) {
      const field = document.getElementById(focused);
      if (field) field.focus({ preventScroll: true });
    }
  }

  document.addEventListener("htmx:afterSwap", (event) => {
    if (event.target instanceof Element) setUp(event.target);
    restoreKept();
    // An element with data-refocus="<input id>" (a form, or the button that deletes an entry)
    // leaves the cursor in that input after a successful request (which the swap may have
    // replaced), a form emptied: ready for the next entry.
    const sender = event.detail.requestConfig && event.detail.requestConfig.elt;
    if (sender && sender.dataset && sender.dataset.refocus) {
      if (sender instanceof HTMLFormElement) sender.reset();
      const input = document.getElementById(sender.dataset.refocus);
      if (input) input.focus();
    }
  });

  // A tick (or untick) of an item shows at once, struck through or not, before the server has
  // answered; the answer then replaces the category. If the request fails, it is undone (app.js
  // says so). An entry of the checklist shows its own state, the checkbox, and nothing else.
  function strike(checkbox) {
    checkbox.closest("[data-item-id]").classList.toggle("line-through", checkbox.checked);
  }

  // A tap on a checkbox whose last tap is still on its way (a quick double tap): htmx holds the
  // new request back on that checkbox until the answer comes, but the answer replaces the
  // category (or the details), checkbox and all, and htmx then drops what it held for an element
  // that is gone. So the state its user wants is noted here and sent from the new checkbox once
  // it is set up.
  const ticking = new WeakSet(); // checkboxes whose request is on its way
  const heldTicks = new Map(); // checkbox id -> {from: checkbox tapped, checked: wanted state}

  document.addEventListener("htmx:beforeRequest", (event) => {
    // A write to the open item's checklist is on its way: if the modal closes before it is
    // answered, the category's tile has to be fetched all the same (leaveItem).
    const { requestConfig, target } = event.detail;
    const verb = requestConfig && String(requestConfig.verb).toLowerCase();
    if (verb !== "get" && target && target.id === "item-details") detailsChanged = true;
    const checkbox = event.detail.elt;
    if (!checkbox.matches(TICKS)) return;
    if (checkbox.matches(TICK)) strike(checkbox);
    ticking.add(checkbox);
    const held = heldTicks.get(checkbox.id);
    if (held && held.from === checkbox) heldTicks.delete(checkbox.id); // htmx sends it itself
  });

  document.addEventListener(
    "change",
    (event) => {
      const checkbox = event.target;
      if (!(checkbox instanceof Element) || !checkbox.matches(TICKS) || !ticking.has(checkbox)) return;
      heldTicks.set(checkbox.id, { from: checkbox, checked: checkbox.checked });
      if (checkbox.matches(TICK)) strike(checkbox);
    },
    true, // before htmx's own listener on the checkbox, which may send a request at once
  );

  // A write that replaced the details (a tick, rename, delete, new entry or order of the
  // checklist): the item's tile on the board has its old count until the modal closes.
  // The server tells the other devices; this tab's own change it leaves out (realtime.js).
  document.addEventListener("htmx:afterRequest", (event) => {
    const { elt, successful, requestConfig, target } = event.detail;
    ticking.delete(elt);
    const verb = requestConfig && String(requestConfig.verb).toLowerCase();
    if (successful && verb !== "get" && target && target.id === "item-details") detailsChanged = true;
  });

  document.addEventListener("htmx:afterSettle", () => {
    for (const [id, { from, checked }] of heldTicks) {
      const checkbox = document.getElementById(id);
      if (checkbox === from) continue; // not replaced: htmx still sends it from there
      heldTicks.delete(id);
      if (!checkbox || !checkbox.matches(TICKS) || checkbox.checked === checked) continue;
      checkbox.checked = checked;
      htmx.trigger(checkbox, "change");
    }
  });

  function failed(event) {
    const checkbox = event.detail.elt;
    if (checkbox.matches(TICKS) && checkbox.isConnected) {
      checkbox.checked = !checkbox.checked;
      if (checkbox.matches(TICK)) strike(checkbox);
    }
    const config = event.detail.requestConfig;
    if (config && DRAG_REQUEST.test(config.path)) stale(); // put the dragged element back
    if (config && CHECKLIST_ORDER.test(config.path)) {
      nextOrder = null; // the server's order is fetched, and shown instead
      Promise.resolve(refreshDetails()).catch(() => {}); // put the dragged entry back
    }
  }
  document.addEventListener("htmx:responseError", failed);
  document.addEventListener("htmx:sendError", failed);

  // Moves `section` one place up or down among the categories and saves the order.
  function moveCategory(section, direction) {
    const sibling = direction === "up" ? section.previousElementSibling : section.nextElementSibling;
    if (!sibling || !sibling.matches("[data-category-id]")) return;
    const container = section.parentElement;
    container.insertBefore(section, direction === "up" ? sibling : sibling.nextElementSibling);
    saveCategoryOrder(container);
  }

  // A checklist entry's handle (it takes the focus): ArrowUp / ArrowDown move the entry one place
  // and save the order, as a drag does. The keyboard's way to reorder, where there is no drag.
  document.addEventListener("keydown", (event) => {
    if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
    if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    const handle = event.target instanceof Element ? event.target.closest(".checklist-handle") : null;
    const entry = handle && handle.closest("[data-entry-id]");
    if (!entry) return;
    event.preventDefault(); // the page does not scroll
    const up = event.key === "ArrowUp";
    const sibling = up ? entry.previousElementSibling : entry.nextElementSibling;
    if (!sibling) return; // at the end of the list: nothing moves, nothing is sent
    const list = entry.parentElement;
    list.insertBefore(entry, up ? sibling : sibling.nextElementSibling);
    handle.focus(); // moving an element takes the focus from it
    saveChecklistOrder(list);
  });

  document.addEventListener("click", (event) => {
    // <button data-focus="field-id"> (the bottom bar's "New category"): to that field, ready to type.
    const focusButton = event.target.closest("[data-focus]");
    const field = focusButton && document.getElementById(focusButton.dataset.focus);
    if (field) {
      if (field.id === "new-category-name") {
        newCategoryShown = true;
        showNewCategoryForm();
      }
      field.scrollIntoView({ block: "center" });
      field.focus();
      return;
    }
    // A category menu's "Move up" / "Move down": the keyboard's (and a fallback's) way to reorder.
    const moveButton = event.target.closest("[data-move]");
    if (moveButton) {
      const section = moveButton.closest("[data-category-id]");
      moveCategory(section, moveButton.dataset.move);
      // The menu moved with its category: the next press moves it again. At the top (or the
      // bottom) the button is hidden, so the cursor goes to the other one.
      const other = moveButton.dataset.move === "up" ? "down" : "up";
      const next = moveButton.offsetParent === null ? section.querySelector(`[data-move="${other}"]`) : moveButton;
      next.focus();
      return;
    }
    // A category menu's "Move to board…": the menu closes, and the dialog opens for the category.
    const moveToBoard = event.target.closest("[data-move-category-id]");
    if (moveToBoard) {
      const menu = moveToBoard.closest("details");
      menu.open = false;
      moveOpener = menu.querySelector("summary");
      const detail = { id: moveToBoard.dataset.moveCategoryId, label: moveToBoard.dataset.moveCategoryLabel };
      window.dispatchEvent(new CustomEvent("move-category", { detail }));
      return;
    }
    // A category's fold button: hides or shows its items on this device.
    const collapseButton = event.target.closest("[data-collapse]");
    if (collapseButton) {
      const section = collapseButton.closest("[data-category-id]");
      if (collapsedNow === null) collapsedNow = collapsedIds();
      const collapse = !collapsedNow.has(section.dataset.categoryId);
      if (collapse) collapsedNow.add(section.dataset.categoryId);
      else collapsedNow.delete(section.dataset.categoryId);
      saveCollapsed(collapsedNow);
      showCollapsed(section, collapse);
    }
  });

  // Lists that htmx swaps out take their Sortable instance with them (Sortable keeps a registry).
  document.addEventListener("htmx:beforeCleanupElement", (event) => {
    const sortable = Sortable.get(event.target);
    if (sortable) sortable.destroy();
  });

  // The open item's details, fetched into #item-details, which they replace (the slot around it
  // stays: htmx-config swaps outerHTML). Returns htmx's promise of the request.
  // `source` is the element the request comes from, and htmx queues a request behind another one
  // from the same element (resolving its promise at once, so the caller would think it was sent
  // and realtime.js would not see its XHR start): the fetch when the modal opens comes from the
  // slot, the refresh realtime.js asks for from #item-details itself, so one is never queued
  // behind the other. (Two refreshes at a time do not happen: realtime.js sends one at a time.)
  function fetchDetails(source) {
    return htmx.ajax("GET", `/items/${openId}/details`, { source, target: "#item-details" });
  }

  // The refresh of the open item's details after an event named it (realtime.js); nothing when no
  // item is open.
  function refreshDetails() {
    const details = document.getElementById("item-details");
    if (openId === null || !details) return undefined;
    return fetchDetails(details);
  }

  window.easytodoBoard = {
    refresh: refreshBoard,
    reloadSwap: RELOAD_SWAP,
    isBusy: () => dragging || editing,
    isDragging: () => dragging,
    openItemId: () => openId,
    refreshDetails,
  };

  // While the modal is open the page behind it can be neither reached nor scrolled.
  const BEHIND_THE_MODAL = ["body > header", BOARD_CONTENT, "#bottom-bar"];
  function setPageInert(inert) {
    for (const selector of BEHIND_THE_MODAL) {
      const element = document.querySelector(selector);
      if (element) element.inert = inert;
    }
    document.body.classList.toggle("overflow-hidden", inert);
  }

  // Tab and Shift+Tab go round the controls of the modal or dialog (the element the handler is on),
  // never out to the page behind it. Only a control that can take the focus counts: not a disabled
  // one, nor one without a box (hidden), which the browser's own Tab skips too.
  function trapFocus(event) {
    const controls = Array.from(event.currentTarget.querySelectorAll(FOCUSABLE)).filter(
      (control) => !control.matches(":disabled") && control.getClientRects().length > 0,
    );
    if (controls.length === 0) return;
    const first = controls[0];
    const last = controls[controls.length - 1];
    const active = document.activeElement;
    // The panel itself (tabindex -1: a click on text in it leaves the focus there) is no Tab stop,
    // but Shift+Tab from it must not leave for the page behind either.
    if (event.shiftKey && (active === first || active === event.currentTarget)) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && active === last) {
      event.preventDefault();
      first.focus();
    }
  }

  // What opening an item does in the modal and in the dialog alike: the details are fetched into
  // the slot (an empty #item-details holds the place for the answer), the board is held (live
  // updates wait, as a new #board-content would not be inert), and the page behind is inert.
  function enterItem(component, itemId) {
    component.itemId = itemId;
    openId = Number(itemId);
    detailsChanged = false;
    const placeholder = document.createElement("div");
    placeholder.id = "item-details";
    component.$refs.details.replaceChildren(placeholder);
    Promise.resolve(fetchDetails(component.$refs.details)).catch(() => {}); // it shows its own error
    component.open = true;
    editing = true;
    setPageInert(true);
  }

  // The reverse of enterItem; the focus goes back to the name that opened it. After a change to
  // the checklist from this tab the item's category is fetched again (its tile shows the count).
  function leaveItem(component) {
    if (!component.open) return;
    component.open = false;
    editing = false;
    openId = null;
    component.$refs.details.replaceChildren();
    setPageInert(false);
    const item = itemElement(component.itemId);
    if (detailsChanged && item) {
      const categoryId = Number(item.closest("[data-category-id]").dataset.categoryId);
      document.dispatchEvent(new CustomEvent("board:changed", { detail: { categoryId } }));
    }
    detailsChanged = false;
    const opener = document.getElementById(`item-${component.itemId}-name`);
    if (opener) opener.focus();
    becameIdle();
  }

  document.addEventListener("alpine:init", () => {
    Alpine.data("itemModal", () => ({
      open: false,
      itemId: "",
      original: {},

      // The modal's hidden attribute (:hidden in the template), which Alpine sets as soon as
      // `open` changes: in the same task as the page behind turns inert, or back. x-show would
      // wait for the next animation frame, which a busy phone can be slow to draw: a tap meanwhile
      // landed on the inert page, the modal then opened over it, and Name never got the cursor.
      get closed() {
        return !this.open;
      },

      // @click on an item's name. Values go into the fields as text (.value, new Option).
      openItem(event) {
        const button = event.currentTarget;
        const item = button.closest("[data-item-id]");
        const { name, description, category } = this.$refs;
        this.original = {
          name: button.dataset.name,
          description: button.dataset.description || "",
          categoryId: item.closest("[data-category-id]").dataset.categoryId,
        };
        name.value = this.original.name;
        description.value = this.original.description;
        category.replaceChildren(
          ...Array.from(
            document.querySelectorAll("[data-category-name]"),
            (el) => new Option(el.dataset.categoryName, el.dataset.categoryId),
          ),
        );
        category.value = this.original.categoryId;
        enterItem(this, item.dataset.itemId);
        this.$nextTick(() => name.focus());
      },

      changed() {
        const { name, description, category } = this.$refs;
        const { original } = this;
        return (
          name.value !== original.name ||
          description.value !== original.description ||
          category.value !== original.categoryId
        );
      },

      trapFocus,

      // A tap on the dimmed page around the modal (on a phone, often meant to hide the keyboard):
      // closes it, but asks first when that would throw edits away.
      dismiss() {
        if (this.changed() && !window.confirm("Discard your changes to this item?")) return;
        this.close();
      },

      close() {
        leaveItem(this);
      },

      // Saves the name and description (if changed), then moves the item (if another category
      // was picked). An item that is no longer on the page was deleted, or the board reloaded.
      save() {
        const { itemId, original } = this;
        const { form, name, description, category } = this.$refs;
        const item = itemElement(itemId);
        if (item) {
          let saved = Promise.resolve();
          if (name.value !== original.name || description.value !== original.description) {
            saved = htmx.ajax("PATCH", `/items/${itemId}`, {
              source: form,
              target: item.closest("[data-category-id]"),
            });
          }
          const categoryId = category.value;
          saved
            .then(() => {
              const current = itemElement(itemId); // the save swapped the category
              if (categoryId !== original.categoryId && current) return moveItem(itemId, categoryId, current);
            })
            .catch(stale); // no answer: what the page shows may not be what was saved
        }
        this.close();
      },

      remove() {
        if (!window.confirm("Delete this item?")) return;
        const item = itemElement(this.itemId);
        if (item) {
          htmx.ajax("DELETE", `/items/${this.itemId}`, {
            source: item,
            target: item.closest("[data-category-id]"),
          });
        }
        this.close();
      },
    }));

    // The dialog that moves a category, with its items, to another board (_move_category.html): the
    // category's menu opens it ("move-category" on the window), and it holds live updates back and
    // makes the page behind inert while it is open, as the item modal does. The request swaps the
    // category's section for the empty answer, which removes it from this board's page.
    Alpine.data("moveCategory", () => ({
      open: false,
      categoryId: "",
      label: "",
      taken: false, // the target board has a category of that name: the field holds a free one
      sending: false, // the request is on its way: one at a time

      get closed() {
        return !this.open;
      },

      // @move-category.window. The values go into the dialog as text (x-text, .value).
      openFor(event) {
        const { id, label } = event.detail;
        const { board, name } = this.$refs;
        this.categoryId = id;
        this.label = label;
        this.taken = false;
        this.sending = false;
        board.selectedIndex = 0;
        name.value = label;
        this.open = true;
        editing = true;
        setPageInert(true);
        this.$nextTick(() => board.focus());
      },

      trapFocus,

      close() {
        if (!this.open) return;
        this.open = false;
        editing = false;
        setPageInert(false);
        const opener = moveOpener;
        moveOpener = null;
        // Not there after a move (the category's section is gone with its menu).
        if (opener && opener.isConnected) opener.focus();
        becameIdle();
      },

      // @submit.prevent on the form: to the board picked, under the name typed.
      move() {
        if (this.sending) return;
        const section = document.getElementById(`category-${this.categoryId}`);
        if (!section) {
          this.close(); // not on the page (any more): what it shows is out of date
          stale();
          return;
        }
        this.taken = false;
        this.sending = true;
        Promise.resolve(
          htmx.ajax("POST", `/categories/${this.categoryId}/move`, { source: this.$refs.form, target: section }),
        ).catch(() => {}); // a failed request shows its own error
      },

      // @htmx:after-request, from the form. The dialog closes when the category has gone, or has
      // gone elsewhere (a 403 or 404 answer: the toast says why, and the board is reloaded), and
      // stays for a name that is taken (409), a refused input (422) or no answer at all.
      afterRequest(event) {
        if (!this.sending) return; // the answer to a request of a dialog closed (or reopened) since
        this.sending = false;
        const { successful, xhr } = event.detail;
        const status = xhr ? xhr.status : 0;
        if (successful || status === 403 || status === 404) this.close();
      },

      // @category-name-taken (the 409's HX-Trigger, which htmx sends in kebab-case too): the field
      // gets the free name the server found, selected so that typing replaces it.
      nameTaken(event) {
        const { name } = this.$refs;
        this.taken = true;
        name.value = event.detail.suggestion;
        name.focus();
        name.select();
      },
    }));

    // The dialog of a member who may not edit: the item's name and description (as text, from the
    // data-* attributes of the name that was clicked) and its details; nothing to change.
    Alpine.data("itemViewer", () => ({
      open: false,
      itemId: "",

      get closed() {
        return !this.open;
      },

      openItem(event) {
        const button = event.currentTarget;
        const { title, description, close } = this.$refs;
        const text = button.dataset.description || "";
        title.textContent = button.dataset.name;
        description.textContent = text;
        description.hidden = text === "";
        enterItem(this, button.closest("[data-item-id]").dataset.itemId);
        this.$nextTick(() => close.focus());
      },

      trapFocus,

      close() {
        leaveItem(this);
      },
    }));
  });
})();
