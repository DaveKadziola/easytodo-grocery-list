const socket = io("http://192.168.100.18:8100", {
  reconnection: true,
  transports: ["websocket"],
  upgrade: false,
});

socket.on("connect", () => {
  console.log("Connected to WebSocket");
});

socket.on("update", (data) => {
  console.log("Received an update:", data);
  if (data.category_id) {
    getTasksByCategory(data.category_id, data.task_id, true);
  }
});

/*
socket.on('connection_status', (data) => {
    console.log('Status połączenia:', data.status);
    console.log('SID:', data.sid);
});

socket.onmessage = function(event) {
    const data = JSON.parse(event.data);
    if (data.type === 'update') {
        fetch(`/get_tasks_by_category/${data.category_id}`)
            .then(response => response.json())
            .then(tasks => updateTasksView(data.category_id, tasks))
            .catch(error => console.error('Błąd aktualizacji:', error));
    }
};

socket.on('update', (data) => {
    console.log('Otrzymano aktualizację:', data);
    if (data.type === 'task_toggled') {
        const checkbox = document.querySelector(`#task-${data.task_id} input[type="checkbox"]`);
        if (checkbox) {
            checkbox.checked = data.is_done;
        }
    }
});*/
