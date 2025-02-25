function openTaskModal(categoryId, taskId, name, description) {
  loadCategories(categoryId);

  document.getElementById("currentCategoryId").value = categoryId;
  document.getElementById("currentTaskId").value = taskId;
  document.getElementById("taskName").value = name;
  document.getElementById("oldTaskName").value = name;
  document.getElementById("taskDescription").value = description;
  document.getElementById("oldTaskDescription").value = description;
  document.getElementById("categoryList").value = categoryId;

  let taskModal = new bootstrap.Modal(document.getElementById("taskModal"));
  taskModal.show();
}

//to fix handling task change and cat change
function saveTask() {
  const categoryId = document.getElementById("currentCategoryId").value;
  const taskId = document.getElementById("currentTaskId").value;
  const oldTaskName = document.getElementById("oldTaskName").value;
  const newTaskName = document.getElementById("taskName").value;
  const oldTaskDescription = document.getElementById("oldTaskDescription").value;
  const newTaskDescription = document.getElementById("taskDescription").value;

  if ((oldTaskName === newTaskName && oldTaskDescription === newTaskDescription) || newTaskName === "") {
    // Close modal if no changes
    let modalEl = document.getElementById("taskModal");
    let modalInstance = bootstrap.Modal.getInstance(modalEl);
    if (modalInstance) {
      modalInstance.hide();
    }
    return;
  }

  fetch(`/update_task/${taskId}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      name: newTaskName,
      description: newTaskDescription,
    }),
  })
    .then((response) => response.json())
    .then((data) => {
      if (data.status === "success") {
        document.getElementById("currentCategoryId").removeAttribute("value");
        document.getElementById("currentTaskId").removeAttribute("value");
        document.getElementById("taskName").removeAttribute("value");
        document.getElementById("oldTaskName").removeAttribute("value");
        document.getElementById("taskDescription").removeAttribute("value");
        let modalEl = document.getElementById("taskModal");
        let modalInstance = bootstrap.Modal.getInstance(modalEl);
        if (modalInstance) {
          modalInstance.hide();
        }

        fetch(`/get_tasks_by_category/${categoryId}`)
          .then((response) => response.json())
          .then((task_list) => {
            console.log("FUNC: updateTask");
            updateTasksView(data.category_id, task_list.tasks);
            setTimeout(() => {
              if (data.task_id) {
                highlightUpdatedTask(data.task_id);
              }
            }, 500);
          })
          .catch((error) => console.error("Błąd pobierania zadań:", error));
      } else {
        alert("Błąd zapisu");
      }
    });

  const selectedElement = document.getElementById("categoryList");
  const selectedElementCategoryId = selectedElement.value;

  // to fix
  if (selectedElementCategoryId !== categoryId) {
    moveTask(taskId, categoryId, selectedElementCategoryId);
  }
}

function deleteTask() {
  const taskId = document.getElementById("currentTaskId").value;
  const categoryId = document.getElementById("currentCategoryId").value;

  if (confirm("Czy napewno chcesz usunąć zadanie?")) {
    fetch(`/delete_task/${taskId}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({}),
    })
      .then((response) => response.json())
      .then((data) => {
        if (data.status === "success") {
          let modalEl = document.getElementById("taskModal");
          let modalInstance = bootstrap.Modal.getInstance(modalEl);
          if (modalInstance) {
            modalInstance.hide();
          }

          console.log("FUNC: deleteTask");
          fetch(`/get_tasks_by_category/${categoryId}`)
            .then((response) => response.json())
            .then((task_list) => {
              console.log("FUNC: deleteTask");
              updateTasksView(categoryId, task_list.tasks);
            })
            .catch((error) => console.error("Błąd pobierania zadań:", error));
        } else {
          alert("Błąd usunięcia");
        }
      });
  }
}

function loadCategories(selectedId) {
  fetch("/get_all_categories/")
    .then((response) => {
      if (!response.ok) throw new Error("Błąd pobierania danych");
      return response.json();
    })
    .then((data) => {
      const select = document.getElementById("categoryList");
      select.innerHTML = "";

      console.log("selectedId ", selectedId);

      data.categories.forEach((category) => {
        const option = new Option(category.name, category.id);
        option.selected = category.id == selectedId;
        select.add(option);
      });
    })
    .catch((error) => {
      console.error("Błąd ładowania kategorii:", error);
      alert("Wystąpił błąd: " + error.message);
    });
}
