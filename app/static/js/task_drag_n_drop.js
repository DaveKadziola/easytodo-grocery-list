// Drag'n'drop for tasks
function dragTask(ev, taskId) {
  ev.dataTransfer.setData("text/plain", taskId);
}

function allowDrop(ev) {
  ev.preventDefault();
}

function dropTask(ev, newCategoryId) {
  ev.preventDefault();
  let taskId = ev.dataTransfer.getData("text/plain");
  fetch("/move_task", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: "task_id=" + encodeURIComponent(taskId) + "&new_category_id=" + encodeURIComponent(newCategoryId),
  })
    .then((response) => response.json())
    .then((data) => {
      if (data.status === "success") {
        location.reload();
      } else {
        alert("Błąd przenoszenia zadania");
      }
    });
}
