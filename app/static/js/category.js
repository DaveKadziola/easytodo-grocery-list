function addCategory() {
  const minWidth = 768;
  const categoryDesktopInput = document.getElementById("newCategoryDesktop");
  const categoryMobileInput = document.getElementById("newCategoryMobile");
  let categoryName = null;

  if (window.innerWidth < minWidth || screen.width < minWidth) {
    categoryName = categoryMobileInput.value.trim();
  } else {
    categoryName = categoryDesktopInput.value.trim();
  }

  if (!categoryName) {
    alert("Nazwa kategorii nie może być pusta");
    return;
  }

  fetch("/add_category", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      category_name: categoryName,
    }),
  })
    .then((response) => response.json())
    .then((data) => {
      if (data.status === "success") {
        categoryDesktopInput.value = "";
        categoryMobileInput.value = "";
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
        updateCategoryMoveButtons();
      } else {
        alert("Błąd dodawania kategorii: " + data.message);
      }
    })
    .catch((error) => {
      console.error("Error:", error);
      alert("Błąd dodawania kategorii: " + error.message);
    });
}

function renameCategory(categoryId, currentName) {
  const newName = prompt("Podaj nową nazwę kategorii", currentName)?.trim();

  if (!newName || newName === currentName) return;

  fetch(`/update_category/${categoryId}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      name: newName,
    }),
  })
    .then((response) => response.json())
    .then((data) => {
      if (data.status === "success") {
        updateCategoryName(categoryId, data.new_name);
      } else {
        throw new Error(data.message || "Błąd aktualizacji kategorii");
      }
    })
    .catch((error) => {
      console.error("Error:", error);
      showNotification(error.message, "error");
    });
}

function deleteCategory(categoryId) {
  if (confirm("Czy napewno chcesz usunąć kategorię?")) {
    fetch("/delete_category/" + categoryId, {
      method: "POST",
    })
      .then((response) => response.json())
      .then((data) => {
        if (data.status === "success") {
          location.reload();
        } else {
          alert("Błąd usunięcia kategorii");
        }
      });
  }
}

// Change category position
function moveCategory(categoryId, direction) {
  fetch("/move_category", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: "category_id=" + encodeURIComponent(categoryId) + "&direction=" + encodeURIComponent(direction),
  })
    .then((response) => response.json())
    .then((data) => {
      if (data.status === "success") {
        window.location.hash = "#category" + categoryId;
        location.reload();
      } else {
        alert("Błąd zmiany kolejności kategorii");
      }
    });
}

function updateCategoryName(categoryId, newName) {
  const categoryLink = document.querySelector(`a[href^="#collapseCategory${categoryId}"]`);

  if (categoryLink) {
    categoryLink.textContent = newName;

    const currentCollapseId = categoryLink.getAttribute("href").replace("#", "");
    const newCollapseId = `collapseCategory${categoryId}`;

    if (currentCollapseId !== newCollapseId) {
      categoryLink.setAttribute("href", `#${newCollapseId}`);
      categoryLink.setAttribute("aria-controls", newCollapseId);
    }
  }
}

function updateCategoryMoveButtons() {
  const categories = document.querySelectorAll(".category-block");

  categories.forEach((category, index) => {
    const upButton = category.querySelector('[onclick*="up"]');
    const downButton = category.querySelector('[onclick*="down"]');

    // Aktualizuj stan przycisków
    upButton.disabled = index === 0;
    downButton.disabled = index === categories.length - 1;

    // Aktualizuj atrybuty Bootstrap
    upButton.classList.toggle("disabled", index === 0);
    downButton.classList.toggle("disabled", index === categories.length - 1);
  });
}
