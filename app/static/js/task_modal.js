function openTaskModal(categoryId, taskId, name, description) {
  document.getElementById("currentCategoryId").value = categoryId;
  document.getElementById("currentTaskId").value = taskId;
  document.getElementById("taskName").value = name;
  document.getElementById("oldTaskName").value = name;
  document.getElementById("taskDescription").value = description;
  document.getElementById("oldTaskDescription").value = description;

  let taskModal = new bootstrap.Modal(document.getElementById("taskModal"));
  taskModal.show();
}

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
