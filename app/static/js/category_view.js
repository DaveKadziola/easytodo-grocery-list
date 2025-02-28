function addCategoryContainer(data) {
  const masonryContainer = document.getElementById("categoriesMasonry");
  const template = document.getElementById("categoryTemplate");
  const newCategory = document.importNode(template.content, true).querySelector(".category-block");

  newCategory.id = `category${data.category_id}`;
  newCategory.setAttribute("ondragstart", `dragCategory(event, ${data.category_id})`);
  newCategory.setAttribute("ondrop", `dropCategory(event, ${data.category_id})`);

  const categoryNameElement = newCategory.querySelector(".card-header a");
  categoryNameElement.textContent = data.category_name;
  categoryNameElement.setAttribute("href", `#collapseCategory${data.category_id}`);
  categoryNameElement.setAttribute("aria-controls", `collapseCategory${data.category_id}`);

  const collapseElement = newCategory.querySelector(".collapse");
  collapseElement.id = `collapseCategory${data.category_id}`;

  const moveUpButton = newCategory.querySelector(".move-btn:first-of-type");
  moveUpButton.setAttribute("onclick", `moveCategory(${data.category_id}, 'up')`);

  const moveDownButton = newCategory.querySelector(".move-btn:last-of-type");
  moveDownButton.setAttribute("onclick", `moveCategory(${data.category_id}, 'down')`);

  const renameLink = newCategory.querySelector(".dropdown-item");
  renameLink.setAttribute("onclick", `renameCategory(${data.category_id}, '${data.category_name}')`);

  const deleteLink = newCategory.querySelector(".dropdown-item.text-danger");
  deleteLink.setAttribute("onclick", `deleteCategory(${data.category_id})`);

  const cardBody = newCategory.querySelector(".card-body");
  cardBody.setAttribute("ondragover", "allowDrop(event)");
  cardBody.setAttribute("ondrop", `dropTask(event, ${data.category_id})`);

  const taskBlocks = newCategory.querySelectorAll(".form-check");
  taskBlocks.forEach((task) => task.remove());

  const addTaskForm = newCategory.querySelector(".form");
  addTaskForm.id = `addTaskForm${data.category_id}`;
  addTaskForm.setAttribute("onsubmit", `event.preventDefault(); addTask(${data.category_id})`);

  const categoryIdInput = addTaskForm.querySelector('input[type="hidden"]');
  categoryIdInput.id = `category_id_${data.category_id}`;
  categoryIdInput.value = data.category_id;

  const newTaskInput = addTaskForm.querySelector('input[type="text"]');
  newTaskInput.id = `newTask_${data.category_id}`;

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

function setWindowViewAtCurrentCategory(action, categoryId) {
  let currentElement;
  let previousElement;
  let nextElement;

  switch (action) {
    case "move":
      sessionStorage.setItem("scrollToCategoryId", categoryId);
      location.reload();
      break;
    case "delete":
      currentElement = document.querySelector("#category" + categoryId + ".category-block");
      previousElement = currentElement.previousElementSibling;
      nextElement = currentElement.nextElementSibling;

      if (previousElement.classList.contains("category-block")) {
        let prevCategoryId = previousElement.id.replace("category", "");
        sessionStorage.setItem("scrollToCategoryId", prevCategoryId);
        currentElement.remove();
        scrollToView();
      } else if (nextElement == null) {
        sessionStorage.setItem("removeLocationHash", true);
        currentElement.remove();
        scrollToView();
      } else {
        let nextCategoryId = nextElement.id.replace("category", "");
        sessionStorage.setItem("scrollToCategoryId", nextCategoryId);
        currentElement.remove();
        scrollToView();
      }
      break;
  }
}

// Scrooling view to last/previous/next category after page reload
// and/or remove location hash from address
document.addEventListener("DOMContentLoaded", scrollToView);

function scrollToView() {
  const categoryId = sessionStorage.getItem("scrollToCategoryId");
  const remLocationHash = sessionStorage.getItem("removeLocationHash");
  if (categoryId) {
    const element = document.getElementById("category" + categoryId);
    if (element) {
      element.scrollIntoView(true);

      // Remove id from sessionStorage after scroll
      sessionStorage.removeItem("scrollToCategoryId");
      removeLocationHash();
    }
  }

  if (remLocationHash) {
    sessionStorage.removeItem("removeLocationHash");
    removeLocationHash();
  }
}
