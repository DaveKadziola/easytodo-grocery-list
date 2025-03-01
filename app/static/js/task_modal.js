function openTaskModal(categoryId, taskId, name, description) {
  getAllCategories(categoryId);

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

function saveTask() {
  const categoryId = document.getElementById("currentCategoryId").value;
  const taskId = document.getElementById("currentTaskId").value;
  const oldTaskName = document.getElementById("oldTaskName").value;
  const newTaskName = document.getElementById("taskName").value;
  const oldTaskDescription = document.getElementById("oldTaskDescription").value;
  const newTaskDescription = document.getElementById("taskDescription").value;
  const selectedElement = document.getElementById("categoryList");
  const selectedElementCategoryId = selectedElement.value;

  // Close modal if no changes else update task details
  if (
    ((oldTaskName === newTaskName && oldTaskDescription === newTaskDescription) || newTaskName === "") &&
    selectedElementCategoryId === categoryId
  ) {
    let modalEl = document.getElementById("taskModal");
    let modalInstance = bootstrap.Modal.getInstance(modalEl);
    if (modalInstance) {
      modalInstance.hide();
    }
    return;
  } else {
    updateTask(categoryId, taskId, true, newTaskName, newTaskDescription);
  }

  if (selectedElementCategoryId !== categoryId) {
    moveTask(taskId, categoryId, selectedElementCategoryId);
  }
}
