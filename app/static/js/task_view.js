function updateTasksView(categoryId, tasks) {
  const cardBody = document.querySelector(`#category${categoryId} .card-body`);
  const taskForm = document.querySelector(`#addTaskForm${categoryId}`);
  const animationDuration = 300;

  // Hide animation
  cardBody.style.transition = `opacity ${animationDuration}ms`;
  cardBody.style.opacity = "0";

  setTimeout(() => {
    // Remove checked/unchecked task and keep form only
    const childrenToRemove = Array.from(cardBody.children).filter((child) => child.id !== `addTaskForm${categoryId}`);
    childrenToRemove.forEach((child) => child.remove());

    // Sort tasks
    tasks.sort((a, b) => a.is_done - b.is_done || a.name.localeCompare(b.name));

    // Add tasks before form
    tasks.forEach((task) => {
      const taskDiv = document.createElement("div");
      taskDiv.className = "form-check mb-2";
      taskDiv.id = `taskBlock${task.id}`;
      taskDiv.draggable = true;
      taskDiv.ondragstart = (e) => dragTask(e, task.id, categoryId);

      const checkbox = document.createElement("input");
      checkbox.className = "form-check-input";
      checkbox.type = "checkbox";
      checkbox.id = `taskCheckbox${task.id}`;
      checkbox.checked = task.is_done;
      checkbox.onchange = () => toggleTask(task.id);

      const span = document.createElement("span");
      span.className = `form-check-label task-item ${task.is_done ? "task-done" : ""}`;
      span.onclick = (event) => {
        openTaskModal(categoryId, task.id, task.name, task.description);
        event.stopPropagation();
      };

      const colDiv = document.createElement("div");
      colDiv.className = "col";
      colDiv.textContent = task.name;

      span.appendChild(colDiv);
      taskDiv.appendChild(checkbox);
      taskDiv.appendChild(span);
      cardBody.insertBefore(taskDiv, taskForm);
    });

    // Show up animation
    cardBody.style.opacity = "1";
  }, animationDuration);
}

function highlightUpdatedTask(highlightFlag, taskId) {
  if (highlightFlag) {
    const taskElement = document.getElementById(`taskBlock${taskId}`);
    if (!taskElement) {
      console.error(`Element #task-${taskId} nie istnieje`);
      return;
    }

    // Reset animation
    taskElement.classList.remove("task-highlight");

    // Apply animationl
    taskElement.classList.add("task-highlight");

    taskElement.addEventListener(
      "animationend",
      () => {
        taskElement.classList.remove("task-highlight");
      },
      { once: true }
    );
  }
}
