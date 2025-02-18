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
  let categoryId = document.getElementById("currentCategoryId").value;
  let taskId = document.getElementById("currentTaskId").value;
  let oldTaskName = document.getElementById("oldTaskName").value;
  let newTaskName = document.getElementById("taskName").value;
  let oldTaskDescription = document.getElementById("oldTaskDescription").value;
  let newTaskDescription = document.getElementById("taskDescription").value;

  if ((oldTaskName === newTaskName && oldTaskDescription === newTaskDescription) || newTaskName === "") {
    // Jeśli nazwa jest pusta, zamknij modal bez zmiany
    let modalEl = document.getElementById("taskModal");
    let modalInstance = bootstrap.Modal.getInstance(modalEl);
    if (modalInstance) {
      modalInstance.hide();
    }
    return;
  }
  let formData = new FormData(document.getElementById("taskForm"));
  fetch("/update_task/" + taskId, {
    method: "POST",
    body: formData,
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
          .then((tasks) => {
            console.log("FUNC: updateTask");
            updateTasksView(data.category_id, tasks);
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
  let taskId = document.getElementById("currentTaskId").value;
  let categoryId = document.getElementById("currentCategoryId").value;
  if (confirm("Czy napewno chcesz usunąć zadanie?")) {
    let formData = new FormData(document.getElementById("taskForm"));
    fetch("/delete_task/" + taskId, {
      method: "POST",
      body: formData,
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
            .then((tasks) => {
              console.log("FUNC: deleteTask");
              updateTasksView(categoryId, tasks);
            })
            .catch((error) => console.error("Błąd pobierania zadań:", error));
        } else {
          alert("Błąd usunięcia");
        }
      });
  }
}
