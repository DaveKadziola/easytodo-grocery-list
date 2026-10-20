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

  moveTask(taskId, oldCategoryId, newCategoryId);
}
