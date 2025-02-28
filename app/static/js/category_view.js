function addCategoryContainer(categoryId, categoryName) {
  const masonryContainer = document.getElementById("categoriesMasonry");
  const template = document.getElementById("categoryTemplate");
  const newCategory = document.importNode(template.content, true).querySelector(".category-block");

  newCategory.id = `category${categoryId}`;
  newCategory.setAttribute("ondragstart", `dragCategory(event, ${categoryId})`);
  newCategory.setAttribute("ondrop", `dropCategory(event, ${categoryId})`);

  const categoryNameElement = newCategory.querySelector(".card-header a");
  categoryNameElement.textContent = categoryName;
  categoryNameElement.setAttribute("href", `#collapseCategory${categoryId}`);
  categoryNameElement.setAttribute("aria-controls", `collapseCategory${categoryId}`);

  const collapseElement = newCategory.querySelector(".collapse");
  collapseElement.id = `collapseCategory${categoryId}`;

  const moveUpButton = newCategory.querySelector(".move-btn:first-of-type");
  moveUpButton.setAttribute("onclick", `moveCategory(${categoryId}, 'up')`);

  const moveDownButton = newCategory.querySelector(".move-btn:last-of-type");
  moveDownButton.setAttribute("onclick", `moveCategory(${categoryId}, 'down')`);

  const renameLink = newCategory.querySelector(".dropdown-item");
  renameLink.setAttribute("onclick", `renameCategory(${categoryId}, '${categoryName}')`);

  const deleteLink = newCategory.querySelector(".dropdown-item.text-danger");
  deleteLink.setAttribute("onclick", `deleteCategory(${categoryId})`);

  const cardBody = newCategory.querySelector(".card-body");
  cardBody.setAttribute("ondragover", "allowDrop(event)");
  cardBody.setAttribute("ondrop", `dropTask(event, ${categoryId})`);

  const taskBlocks = newCategory.querySelectorAll(".form-check");
  taskBlocks.forEach((task) => task.remove());

  const addTaskForm = newCategory.querySelector(".form");
  addTaskForm.id = `addTaskForm${categoryId}`;
  addTaskForm.setAttribute("onsubmit", `event.preventDefault(); addTask(${categoryId})`);

  const categoryIdInput = addTaskForm.querySelector('input[type="hidden"]');
  categoryIdInput.id = `category_id_${categoryId}`;
  categoryIdInput.value = categoryId;

  const newTaskInput = addTaskForm.querySelector('input[type="text"]');
  newTaskInput.id = `newTask_${categoryId}`;

  const newTaskButton = addTaskForm.querySelector('button[class="btn btn-outline-light"]');
  newTaskButton.setAttribute("type", "submit");

  masonryContainer.appendChild(newCategory);
  updateViewCategoryMoveButtons();
}

function updateViewCategoryName(categoryId, newName) {
  const categoryLink = document.querySelector(`a[href^="#collapseCategory${categoryId}"]`);

  if (categoryLink) {
    categoryLink.textContent = newName;

    const currentCollapseId = categoryLink.getAttribute("href").replace("#", "");
    const newCollapseId = `collapseCategory${categoryId}`;

    if (currentCollapseId !== newCollapseId) {
      categoryLink.setAttribute("href", `#${newCollapseId}`);
      categoryLink.setAttribute("aria-controls", newCollapseId);
    }
    removeLocationHash();
  }
}

function updateViewCategoryMoveButtons() {
  const categories = document.querySelectorAll(".category-block");

  categories.forEach((category, index) => {
    const upButton = category.querySelector('[onclick*="up"]');
    const downButton = category.querySelector('[onclick*="down"]');

    // Update button state
    upButton.disabled = index === 0;
    downButton.disabled = index === categories.length - 1;

    // Update Bootstrap attributes
    upButton.classList.toggle("disabled", index === 0);
    downButton.classList.toggle("disabled", index === categories.length - 1);
  });
}

function setWindowViewAtCurrentCategory(action, categoryId, direction) {
  let currentElement = document.querySelector(`#category${categoryId}.category-block`);
  let previousElement = currentElement.previousElementSibling;
  let nextElement = currentElement.nextElementSibling;

  switch (action) {
    case "move":
      if (direction === "up" && previousElement) {
        currentElement.parentNode.insertBefore(currentElement, previousElement);
      } else if (direction === "down" && nextElement) {
        currentElement.parentNode.insertBefore(nextElement, currentElement);
      }

      updateViewCategoryMoveButtons();
      scrollToView(categoryId);
      break;
    case "delete":
      if (previousElement.classList.contains("category-block")) {
        let prevCategoryId = previousElement.id.replace("category", "");
        currentElement.remove();
        scrollToView(prevCategoryId);
      } else if (nextElement == null) {
        sessionStorage.setItem("removeLocationHash", true);
        currentElement.remove();
        scrollToView(null);
      } else {
        let nextCategoryId = nextElement.id.replace("category", "");
        currentElement.remove();
        updateViewCategoryMoveButtons();
        scrollToView(nextCategoryId);
      }
      break;
  }
}

// Scrooling view to last/previous/next category after page reload
// and/or remove location hash from address
document.addEventListener("DOMContentLoaded", scrollToView);

function scrollToView(categoryId) {
  const remLocationHash = sessionStorage.getItem("removeLocationHash");
  if (categoryId) {
    const element = document.getElementById("category" + categoryId);
    if (element) {
      element.scrollIntoView(true);

      // Remove id from sessionStorage after scroll
      removeLocationHash();
    }
  }

  if (remLocationHash) {
    sessionStorage.removeItem("removeLocationHash");
    removeLocationHash();
  }
}
