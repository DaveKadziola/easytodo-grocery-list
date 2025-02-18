function addTask(categoryId) {
  const taskInput = document.getElementById("newTask_" + categoryId);
  const taskName = taskInput.value.trim();

  if (!taskName) {
    alert("Nazwa zadania nie może być pusta");
    return;
  }

  const data = {
    category_id: categoryId,
    task_name: taskName,
  };

  console.log("FUNC: addTask cat id: " + categoryId);

  fetch("/add_task", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Requested-With": "XMLHttpRequest",
    },
    body: JSON.stringify(data),
  })
    .then((response) => response.json())
    .then((data) => {
      if (data.status === "success") {
        taskInput.value = "";
        fetch(`/get_tasks_by_category/${categoryId}`)
          .then((response) => response.json())
          .then((tasks) => {
            console.log("FUNC: addTask");
            updateTasksView(data.category_id, tasks);
            setTimeout(() => {
              if (data.task_id) {
                highlightUpdatedTask(data.task_id);
              }
            }, 500);
          })
          .catch((error) => console.error("Błąd pobierania zadań:", error));
      } else {
        throw new Error("Błąd aktualizacji statusu");
      }
    })
    .catch((error) => {
      console.error("Error:", error);
      checkbox.checked = !checkbox.checked;
      alert("Wystąpił błąd: " + error.message);
    });
}

// Functions for task handling
function toggleTask(taskId) {
  let checkbox = document.getElementById("taskCheckbox" + taskId);
  let categoryBlock = checkbox.closest(".category-block");
  let categoryId = categoryBlock.id.replace("category", "");

  fetch("/toggle_task/" + taskId, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ is_done: checkbox.checked }),
  })
    .then((response) => response.json())
    .then((data) => {
      if (data.status === "success") {
        fetch(`/get_tasks_by_category/${categoryId}`)
          .then((response) => response.json())
          .then((tasks) => {
            updateTasksView(data.category_id, tasks);
            setTimeout(() => {
              if (data.task_id) {
                highlightUpdatedTask(data.task_id);
              }
            }, 500);
          })
          .catch((error) => console.error("Błąd pobierania zadań:", error));
      } else {
        throw new Error("Błąd aktualizacji statusu");
      }
    })
    .catch((error) => {
      console.error("Error:", error);
      checkbox.checked = !checkbox.checked;
      alert("Wystąpił błąd: " + error.message);
    });
}

function updateTasksView(categoryId, tasks) {
  const cardBody = document.querySelector(`#category${categoryId} .card-body`);
  const taskForm = document.querySelector(`#addTaskForm${categoryId}`); // Wybierz formularz po ID
  const animationDuration = 300;

  console.log("FUNC: updateTasksView");

  // Animacja zanikania
  cardBody.style.transition = `opacity ${animationDuration}ms`;
  cardBody.style.opacity = "0";

  setTimeout(() => {
    // Usuń tylko zadania, zachowując formularz
    const childrenToRemove = Array.from(cardBody.children).filter((child) => child.id !== `addTaskForm${categoryId}`);
    childrenToRemove.forEach((child) => child.remove());

    // Sortowanie zadań
    tasks.sort((a, b) => a.is_done - b.is_done || a.name.localeCompare(b.name));

    // Dodawanie zadań przed formularzem
    tasks.forEach((task) => {
      const taskDiv = document.createElement("div");
      taskDiv.className = "form-check mb-2";
      taskDiv.id = `taskBlock${task.id}`;
      taskDiv.draggable = true;
      taskDiv.ondragstart = (e) => dragTask(e, task.id);

      const checkbox = document.createElement("input");
      checkbox.className = "form-check-input";
      checkbox.type = "checkbox";
      checkbox.id = `taskCheckbox${task.id}`;
      checkbox.checked = task.is_done;
      checkbox.onchange = () => toggleTask(task.id);

      const span = document.createElement("span");
      span.className = `form-check-label task-item ${task.is_done ? "task-done" : ""}`;
      span.onclick = (e) => {
        e.stopPropagation();
        openTaskModal(categoryId, task.id, task.name, task.description);
      };

      const colDiv = document.createElement("div");
      colDiv.className = "col";
      colDiv.textContent = task.name;

      span.appendChild(colDiv);
      taskDiv.appendChild(checkbox);
      taskDiv.appendChild(span);
      cardBody.insertBefore(taskDiv, taskForm);
    });

    // Animacja pojawiania się
    cardBody.style.opacity = "1";
  }, animationDuration);

  console.log(`Aktualizacja kategorii ${categoryId}`, tasks);
}

function highlightUpdatedTask(taskId) {
  const taskElement = document.getElementById(`taskBlock${taskId}`);
  if (!taskElement) {
    console.error(`Element #task-${taskId} nie istnieje`);
    return;
  }

  // Reset animacji
  taskElement.classList.remove("task-highlight");

  taskElement.classList.add("task-highlight");

  taskElement.addEventListener(
    "animationend",
    () => {
      taskElement.classList.remove("task-highlight");
    },
    { once: true }
  );
}
