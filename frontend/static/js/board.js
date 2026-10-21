// Board page: drag and drop (SortableJS) and Move up / Move down for categories, the item modal
// (Alpine component "itemModal"), ticks that show at once, fields that are ready for the next
// entry and keep what the user typed when their category is swapped, folding categories away (per
// device), the bottom bar's "New category", and a board reload when a change did not go through.
// Loaded before Alpine (see base.html), so the component is registered on alpine:init. The modal
// reads items only from data-* attributes, which the templates escape; no page data is ever
// written into a script.
// For realtime.js: window.easytodoBoard (the board reload and its swap style, and whether a drag
// or the modal is in progress), a "board:idle" event on the document when that ends, and a
// "board:stale" event when the page no longer shows what the server has (it then queues a reload).
(() => {
  "use strict";

  const BOARD_CONTENT = "#board-content";
  const TICK = 'input[type="checkbox"][hx-post$="/toggle"]';
  const FOCUSABLE = "input, textarea, select, button";
  const TEXT_FIELDS = "input[id]:not([type=checkbox]):not([type=hidden]), textarea[id]";
  // Requests whose failure leaves the page out of step: Sortable has already moved the element.
  const DRAG_REQUEST = /\/items\/\d+\/move$|\/categories\/order$/;
  const COLLAPSED_KEY = "easytodo.collapsedCategories";
  // A reload that the user did not ask for just now: htmx sets the new content up at once (no
  // settle delay), so a form in it never submits as a plain page request before htmx has it.
  const RELOAD_SWAP = "outerHTML settle:0ms";
  let dragging = false; // a Sortable drag is running
  let editing = false; // the item modal is open
  let newCategoryShown = false; // on a phone the bottom bar's button showed the "New category" form

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
    if (elt.matches(TICK)) parameters.done = String(elt.checked); // the state its user wants
    if (elt instanceof HTMLFormElement) {
      const sent = {};
      for (const field of elt.querySelectorAll(TEXT_FIELDS)) sent[field.id] = field.value;
      sentValues.set(elt, sent);
    }
  });

  document.addEventListener("htmx:beforeSwap", (event) => {
    kept = null;
    const { target, requestConfig, xhr } = event.detail;
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
    // A form with data-refocus="<input id>" is ready for the next entry after a successful
    // submit: emptied, with the cursor in that input (which the swap may have replaced).
    const form = event.detail.requestConfig && event.detail.requestConfig.elt;
    if (form instanceof HTMLFormElement && form.dataset.refocus) {
      form.reset();
      const input = document.getElementById(form.dataset.refocus);
      if (input) input.focus();
    }
  });

  // A tick (or untick) shows at once, struck through or not, before the server has answered;
  // the answer then replaces the category. If the request fails, it is undone (app.js says so).
  function strike(checkbox) {
    checkbox.closest("[data-item-id]").classList.toggle("line-through", checkbox.checked);
  }

  // A tap on a checkbox whose last tap is still on its way (a quick double tap): htmx holds the
  // new request back on that checkbox until the answer comes, but the answer replaces the
  // category, checkbox and all, and htmx then drops what it held for an element that is gone. So
  // the state its user wants is noted here and sent from the new checkbox once it is set up.
  const ticking = new WeakSet(); // checkboxes whose request is on its way
  const heldTicks = new Map(); // checkbox id -> {from: checkbox tapped, checked: wanted state}

  document.addEventListener("htmx:beforeRequest", (event) => {
    const checkbox = event.detail.elt;
    if (!checkbox.matches(TICK)) return;
    strike(checkbox);
    ticking.add(checkbox);
    const held = heldTicks.get(checkbox.id);
    if (held && held.from === checkbox) heldTicks.delete(checkbox.id); // htmx sends it itself
  });

  document.addEventListener(
    "change",
    (event) => {
      const checkbox = event.target;
      if (!(checkbox instanceof Element) || !checkbox.matches(TICK) || !ticking.has(checkbox)) return;
      heldTicks.set(checkbox.id, { from: checkbox, checked: checkbox.checked });
      strike(checkbox);
    },
    true, // before htmx's own listener on the checkbox, which may send a request at once
  );

  document.addEventListener("htmx:afterRequest", (event) => ticking.delete(event.detail.elt));

  document.addEventListener("htmx:afterSettle", () => {
    for (const [id, { from, checked }] of heldTicks) {
      const checkbox = document.getElementById(id);
      if (checkbox === from) continue; // not replaced: htmx still sends it from there
      heldTicks.delete(id);
      if (!checkbox || !checkbox.matches(TICK) || checkbox.checked === checked) continue;
      checkbox.checked = checked;
      htmx.trigger(checkbox, "change");
    }
  });

  function failed(event) {
    const checkbox = event.detail.elt;
    if (checkbox.matches(TICK) && checkbox.isConnected) {
      checkbox.checked = !checkbox.checked;
      strike(checkbox);
    }
    const config = event.detail.requestConfig;
    if (config && DRAG_REQUEST.test(config.path)) stale(); // put the dragged element back
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

  window.easytodoBoard = {
    refresh: refreshBoard,
    reloadSwap: RELOAD_SWAP,
    isBusy: () => dragging || editing,
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

  document.addEventListener("alpine:init", () => {
    Alpine.data("itemModal", () => ({
      open: false,
      itemId: "",
      original: {},

      // @click on an item's name. Values go into the fields as text (.value, new Option).
      openItem(event) {
        const button = event.currentTarget;
        const item = button.closest("[data-item-id]");
        const { name, description, category } = this.$refs;
        this.itemId = item.dataset.itemId;
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
        this.open = true;
        editing = true;
        setPageInert(true);
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

      // Tab and Shift+Tab go round the modal's controls, never out to the page behind it.
      trapFocus(event) {
        const controls = Array.from(event.currentTarget.querySelectorAll(FOCUSABLE));
        const first = controls[0];
        const last = controls[controls.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      },

      // A tap on the dimmed page around the modal (on a phone, often meant to hide the keyboard):
      // closes it, but asks first when that would throw edits away.
      dismiss() {
        if (this.changed() && !window.confirm("Discard your changes to this item?")) return;
        this.close();
      },

      close() {
        if (!this.open) return;
        this.open = false;
        editing = false;
        setPageInert(false);
        const opener = document.getElementById(`item-${this.itemId}-name`);
        if (opener) opener.focus();
        becameIdle();
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
  });
})();
