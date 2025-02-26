function addTask(categoryId) {
  const taskInput = document.getElementById("newTask_" + categoryId);
  const taskName = taskInput.value.trim();

  if (!taskName) {
    alert("The task name cannot be empty.");
    return;
  }

  fetch("/add_task/", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      category_id: categoryId,
      task_name: taskName,
    }),
  })
    .then((response) => {
      if (!response.ok) throw new Error("Network error.");
      return response.json();
    })
    .then((data) => {
      if (data.status === "success") {
        taskInput.value = "";
        getTasksByCategory(categoryId, data.task_id, true);
      } else {
        throw new Error("Error adding task.");
      }
    })
    .catch((error) => {
      console.error("Error at addTask:", error);
      alert("Error: " + error.message);
    });
}

function getTasksByCategory(categoryId, taskId, highlightFlag) {
  fetch(`/get_tasks_by_category/${categoryId}`)
    .then((response) => {
      if (!response.ok) throw new Error("Network error.");
      return response.json();
    })
    .then((task_list) => {
      updateTasksView(categoryId, task_list.tasks);
      setTimeout(() => {
        if (taskId) {
          highlightUpdatedTask(highlightFlag, taskId);
        }
      }, 500);
    })
    .catch((error) => {
      console.error("Error at getTasksByCategory:", error);
      alert("Error: " + error.message);
    });
}

function toggleTask(taskId) {
  const checkbox = document.getElementById("taskCheckbox" + taskId);
  const categoryBlock = checkbox.closest(".category-block");
  const categoryId = categoryBlock.id.replace("category", "");

  fetch("/toggle_task/" + taskId, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      is_done: checkbox.checked,
    }),
  })
    .then((response) => {
      if (!response.ok) throw new Error("Network error.");
      return response.json();
    })
    .then((data) => {
      if (data.status === "success") {
        getTasksByCategory(categoryId, taskId, true);
      } else {
        throw new Error("Error updating status.");
      }
    })
    .catch((error) => {
      console.error("Error at toggleTask:", error);
      alert("Error: " + error.message);
    });
}

function updateTask(categoryId, taskId, highlightFlag, newTaskName, newTaskDescription) {
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
    .then((response) => {
      if (!response.ok) throw new Error("Network error.");
      return response.json();
    })
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

        getTasksByCategory(categoryId, taskId, highlightFlag);
      } else {
        alert("Error updating task details.");
      }
    })
    .catch((error) => {
      console.error("Error at updateTask:", error);
      alert("Error: " + error.message);
    });
}

function moveTask(taskId, oldCategoryId, newCategoryId) {
  fetch("/move_task", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      task_id: taskId,
      new_category_id: newCategoryId,
    }),
  })
    .then((response) => {
      if (!response.ok) throw new Error("Network error.");
      return response.json();
    })
    .then((data) => {
      if (data.status === "success") {
        // Refresh previous task category
        getTasksByCategory(oldCategoryId, taskId, false);

        // Refresh new task category
        getTasksByCategory(newCategoryId, taskId, true);
      } else {
        alert("Error moving task.");
      }
    })
    .catch((error) => {
      console.error("Error at moveTask:", error);
      alert("Error: " + error.message);
    });
}

function deleteTask() {
  const taskId = document.getElementById("currentTaskId").value;
  const categoryId = document.getElementById("currentCategoryId").value;

  if (confirm("Are you sure you want to delete this task?")) {
    fetch(`/delete_task/${taskId}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({}),
    })
      .then((response) => {
        if (!response.ok) throw new Error("Network error.");
        return response.json();
      })
      .then((data) => {
        if (data.status === "success") {
          let modalEl = document.getElementById("taskModal");
          let modalInstance = bootstrap.Modal.getInstance(modalEl);
          if (modalInstance) {
            modalInstance.hide();
          }

          getTasksByCategory(categoryId, taskId, false);
        } else {
          alert("Error deleting task.");
        }
      })
      .catch((error) => {
        console.error("Error at deleteTask:", error);
        alert("Error: " + error.message);
      });
  }
}
