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
    alert("The category name cannot be empty!");
    return;
  }

  fetch("/v1/add_category", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "application/vnd.myapi.v1+json",
    },
    body: JSON.stringify({
      category_name: categoryName,
    }),
  })
    .then(async (response) => {
      const data = await response.json();

      if (!response.ok) {
        const error = new Error(data.message || "Request failed");
        error.status = response.status;
        error.details = data;
        throw error;
      }

      return data;
    })
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
      } else {
        alert(error.status + ": " + error.message);
      }
    })
    .catch((error) => {
      console.error("Error at adding category:", error);
      alert(error.status + ": " + error.message);
    });
}

function renameCategory(categoryId) {
  const currentName = document.querySelector(`a[href^="#collapseCategory${categoryId}"]`).textContent.trim();
  const newName = prompt("Enter a new category name:", currentName)?.trim();

  if (!newName || newName === currentName) return;

  fetch(`/v1/rename_category/${categoryId}`, {
    method: "PUT",
    headers: {
      "Content-Type": "application/json",
      Accept: "application/vnd.myapi.v1+json",
    },
    body: JSON.stringify({
      name: newName,
    }),
  })
    .then(async (response) => {
      const data = await response.json();

      if (!response.ok) {
        const error = new Error(data.message || "Request failed");
        error.status = response.status;
        error.details = data;
        throw error;
      }

      return data;
    })
    .then((data) => {
      if (data.status === "success") {
        updateViewCategoryName(categoryId, data.new_name);
      } else {
        throw new Error(data.message || "Error updating category.");
      }
    })
    .catch((error) => {
      console.error("Error at renameCategory:", error);
      alert(error.status + ": " + error.message);
    });
}

function deleteCategory(categoryId) {
  if (confirm("Are you sure you want to delete this category?")) {
    fetch(`/v1/delete_category/${categoryId}`, {
      method: "DELETE",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/vnd.myapi.v1+json",
      },
      body: JSON.stringify({}),
    })
      .then(async (response) => {
        const data = await response.json();

        if (!response.ok) {
          const error = new Error(data.message || "Request failed");
          error.status = response.status;
          error.details = data;
          throw error;
        }

        return data;
      })
      .then((data) => {
        if (data.status === "success") {
          setWindowViewAtCurrentCategory("delete", categoryId);
        } else {
          throw new Error(data.message || "Error deleting category.");
        }
      })
      .catch((error) => {
        console.error("Error at deleteCategory:", error);
        alert(error.status + ": " + error.message);
      });
  }
}

function getAllCategories(selectedId) {
  fetch("/v1/get_all_categories/", {
    method: "GET",
    headers: {
      "Content-Type": "application/json",
      Accept: "application/vnd.myapi.v1+json",
    },
  })
    .then(async (response) => {
      const data = await response.json();

      if (!response.ok) {
        const error = new Error(data.message || "Request failed");
        error.status = response.status;
        error.details = data;
        throw error;
      }

      return data;
    })
    .then((data) => {
      const select = document.getElementById("categoryList");
      select.innerHTML = "";

      data.categories.forEach((category) => {
        const option = new Option(category.name, category.id);
        option.selected = category.id == selectedId;
        select.add(option);
      });
    })
    .catch((error) => {
      console.error("Error at getAllCategories:", error);
      alert(error.status + ": " + error.message);
    });
}

function moveCategory(categoryId, direction) {
  fetch("/v1/move_category", {
    method: "PUT",
    headers: {
      "Content-Type": "application/json",
      Accept: "application/vnd.myapi.v1+json",
    },
    body: JSON.stringify({
      category_id: parseInt(categoryId, 10),
      direction: direction,
    }),
  })
    .then(async (response) => {
      const data = await response.json();

      if (!response.ok) {
        const error = new Error(data.message || "Request failed");
        error.status = response.status;
        error.details = data;
        throw error;
      }

      return data;
    })
    .then((data) => {
      if (data.status === "success") {
        setWindowViewAtCurrentCategory("move", categoryId);
      } else {
        throw new Error(data.message || "Category reorder error.");
      }
    })
    .catch((error) => {
      console.error("Error at moveCategory:", error);
      alert(error.status + ": " + error.message);
    });
}
