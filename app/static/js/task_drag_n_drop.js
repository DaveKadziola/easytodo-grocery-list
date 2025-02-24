// Drag'n'drop for tasks
function dragTask(ev, taskId, categoryId) {
  ev.dataTransfer.setData("application/task", taskId);
  ev.dataTransfer.setData("application/category", categoryId);
  console.log("drap taskId: ", taskId);
  console.log("drap categoryId: ", categoryId);
}

function allowDrop(ev) {
  ev.preventDefault();
}

function dropTask(ev, newCategoryId) {
  ev.preventDefault();
  let taskId = ev.dataTransfer.getData("application/task");
  let oldCategoryId = ev.dataTransfer.getData("application/category");

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
    .then((response) => response.json())
    .then((data) => {
      if (data.status === "success") {
        // Refresh previous task category
        fetch(`/get_tasks_by_category/${oldCategoryId}`)
          .then((response) => response.json())
          .then((task_list) => {
            console.log("FUNC: old tasks");
            updateTasksView(oldCategoryId, task_list.tasks);
          })
          .catch((error) => console.error("Błąd pobierania zadań:", error));

        // Refresh new task category
        fetch(`/get_tasks_by_category/${newCategoryId}`)
          .then((response) => response.json())
          .then((task_list) => {
            console.log("FUNC: nrew tasks");
            updateTasksView(newCategoryId, task_list.tasks);
            setTimeout(() => {
              if (taskId) {
                highlightUpdatedTask(taskId);
              }
            }, 500);
          })
          .catch((error) => console.error("Błąd pobierania zadań:", error));
      } else {
        alert("Błąd przenoszenia zadania: " + (data.message || ""));
      }
    })
    .catch((error) => console.error("Błąd:", error));
}
