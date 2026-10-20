// Board page: drag and drop (SortableJS), the item modal (Alpine component "itemModal"), fields
// that are ready for the next entry, and reloading the board when a change lost a race.
// Loaded before Alpine (see base.html), so the component is registered on alpine:init. The modal
// reads items only from data-* attributes, which the templates escape; no page data is ever
// written into a script.
(() => {
  "use strict";

  const BOARD_CONTENT = "#board-content";

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
  // "resync" from its own answer (the board is gone) does not start another one.
  function refreshBoard() {
    const board = boardContent();
    if (board) htmx.ajax("GET", `/boards/${board.dataset.boardId}/content`, { source: board, target: board });
  }

  // `source` must be on the page and not inside a form (its fields would be sent along).
  function moveItem(itemId, categoryId, source) {
    return htmx.ajax("POST", `/items/${itemId}/move`, {
      source,
      target: BOARD_CONTENT,
      values: { category_id: categoryId },
    });
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
        onEnd(event) {
          if (event.from === event.to) return;
          const category = event.to.closest("[data-category-id]");
          moveItem(event.item.dataset.itemId, category.dataset.categoryId, event.item);
        },
      });
    });
    matching(root, "[data-sortable-categories]").forEach((container) => {
      if (Sortable.get(container)) return;
      Sortable.create(container, {
        handle: ".category-handle",
        draggable: "[data-category-id]",
        onEnd(event) {
          if (event.oldIndex === event.newIndex) return;
          const ids = Array.from(container.children, (el) => el.dataset.categoryId).filter(Boolean);
          const boardId = boardContent().dataset.boardId;
          htmx.ajax("PUT", `/boards/${boardId}/categories/order`, {
            source: container,
            swap: "none",
            values: { ids },
          });
        },
      });
    });
  }

  document.addEventListener("DOMContentLoaded", () => initSortable(document));

  document.addEventListener("htmx:afterSwap", (event) => {
    if (event.target instanceof Element) initSortable(event.target);
    // A form with data-refocus="<input id>" is ready for the next entry after a successful
    // submit: emptied, with the cursor in that input (which the swap may have replaced).
    const form = event.detail.requestConfig && event.detail.requestConfig.elt;
    if (form instanceof HTMLFormElement && form.dataset.refocus) {
      form.reset();
      const input = document.getElementById(form.dataset.refocus);
      if (input) input.focus();
    }
  });

  // Lists that htmx swaps out take their Sortable instance with them (Sortable keeps a registry).
  document.addEventListener("htmx:beforeCleanupElement", (event) => {
    const sortable = Sortable.get(event.target);
    if (sortable) sortable.destroy();
  });

  // A 404 answer (the item or category was deleted on another device) triggers "resync": reload
  // the board so nothing stale stays on screen. Not for the board reload's own 404.
  document.addEventListener("resync", (event) => {
    const board = boardContent();
    if (board && event.target !== board) refreshBoard();
  });

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
        this.$nextTick(() => name.focus());
      },

      close() {
        if (!this.open) return;
        this.open = false;
        const opener = document.getElementById(`item-${this.itemId}-name`);
        if (opener) opener.focus();
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
          if (categoryId !== original.categoryId) {
            saved.then(() => {
              const current = itemElement(itemId); // the save swapped the category
              if (current) moveItem(itemId, categoryId, current);
            });
          }
        }
        this.close();
      },

      remove() {
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
