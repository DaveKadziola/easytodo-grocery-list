function addTask(categoryId) {
  const taskInput = document.getElementById("newTask_" + categoryId);
  const taskName = taskInput.value.trim();

  if (!taskName) {
    alert("The task name cannot be empty.");
    return;
  }

  fetch("/v1/add_task/", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "application/vnd.myapi.v1+json",
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
        /* Function already triggered by socket.io, stored only for eventual change testing and have easy access to it
          getTasksByCategory(categoryId, data.task_id, true);
        */
      } else {
        throw new Error("Error adding task.");
      }
    })
    .catch((error) => {
      console.error("Error at addTask:", error);
      alert(error.status + ": " + error.message);
    });
}

function getTasksByCategory(categoryId, taskId, highlightFlag) {
  fetch(`/v1/get_tasks_by_category/${categoryId}`, {
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
      alert(error.status + ": " + error.message);
    });
}

function toggleTask(taskId) {
  const checkbox = document.getElementById("taskCheckbox" + taskId);
  const categoryBlock = checkbox.closest(".category-block");
  const categoryId = categoryBlock.id.replace("category", "");

  fetch("/v1/toggle_task/" + taskId, {
    method: "PUT",
    headers: {
      "Content-Type": "application/json",
      Accept: "application/vnd.myapi.v1+json",
    },
    body: JSON.stringify({
      is_done: checkbox.checked,
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
        /* Function already triggered by socket.io, stored only for eventual change testing and have easy access to it
          getTasksByCategory(categoryId, taskId, true);
        */
      } else {
        throw new Error("Error updating status.");
      }
    })
    .catch((error) => {
      console.error("Error at toggleTask:", error);
      alert(error.status + ": " + error.message);
    });
}

function updateTask(categoryId, taskId, highlightFlag, newTaskName, newTaskDescription) {
  fetch(`/v1/update_task/${taskId}`, {
    method: "PUT",
    headers: {
      "Content-Type": "application/json",
      Accept: "application/vnd.myapi.v1+json",
    },
    body: JSON.stringify({
      name: newTaskName,
      description: newTaskDescription,
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
        /* Function already triggered by socket.io, stored only for eventual change testing and have easy access to it
          getTasksByCategory(categoryId, taskId, highlightFlag);
        */
      } else {
        alert("Error updating task details.");
      }
    })
    .catch((error) => {
      console.error("Error at updateTask:", error);
      alert(error.status + ": " + error.message);
    });
}

function moveTask(taskId, oldCategoryId, newCategoryId) {
  fetch("/v1/move_task", {
    method: "PUT",
    headers: {
      "Content-Type": "application/json",
      Accept: "application/vnd.myapi.v1+json",
    },
    body: JSON.stringify({
      task_id: parseInt(taskId, 10),
      new_category_id: parseInt(newCategoryId, 10),
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
        /* Functions already triggered by socket.io, stored only for eventual change testing and have easy access to it
          // Refresh previous task category
          getTasksByCategory(oldCategoryId, taskId, false);

          // Refresh new task category
          getTasksByCategory(newCategoryId, taskId, true);
        */
      } else {
        alert("Error moving task.");
      }
    })
    .catch((error) => {
      console.error("Error at moveTask:", error);
      alert(error.status + ": " + error.message);
    });
}

function deleteTask() {
  const taskId = document.getElementById("currentTaskId").value;
  const categoryId = document.getElementById("currentCategoryId").value;

  if (confirm("Are you sure you want to delete this task?")) {
    fetch(`/v1/delete_task/${taskId}`, {
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
          let modalEl = document.getElementById("taskModal");
          let modalInstance = bootstrap.Modal.getInstance(modalEl);
          if (modalInstance) {
            modalInstance.hide();
          }
          /* Function already triggered by socket.io, stored only for eventual change testing and have easy access to it
            //getTasksByCategory(categoryId, taskId, false);
          */
        } else {
          alert("Error deleting task.");
        }
      })
      .catch((error) => {
        console.error("Error at deleteTask:", error);
        alert(error.status + ": " + error.message);
      });
  }
}
