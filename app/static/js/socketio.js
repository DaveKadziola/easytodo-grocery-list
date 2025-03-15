let hostName;
let port;
let lastTimestamp;

fetch("/static/js/socketio.json")
  .then((response) => response.json())
  .then((config) => {
    hostName = config.host.name;
    port = config.host.port;
  })
  .catch((error) => {
    console.error("Error loading configuration:", error);
  });

const socket = io("ws://", hostName, ":", port, {
  reconnection: true,
  transports: ["websocket"],
  upgrade: false,
  pingTimeout: 15000,
  pingInterval: 7000,
});

socket.on("connect", () => {
  lastTimestamp = localStorage.getItem("lastTimestamp") || toLocalISOString();
  console.log("Connected to WebSocket");
  socket.emit("request_updates", lastTimestamp);
});

const handleEvent = (data) => {
  switch (data.action) {
    case "ADD_TASK":
      getTasksByCategory(data.category_id, data.task_id, true);
      break;
    case "MOVE_TASK":
      getTasksByCategory(data.old_category_id, data.task_id, false);
      getTasksByCategory(data.new_category_id, data.task_id, true);
      break;
    case "DELETE_TASK":
      getTasksByCategory(data.category_id, data.task_id, false);
      break;
    case "TOGGLE_TASK":
    case "UPDATE_TASK":
      getTasksByCategory(data.category_id, data.task_id, true);
      break;
    case "ADD_CATEGORY":
      addCategoryContainer(data.category_id, data.name);
      break;
    case "RENAME_CATEGORY":
      updateViewCategoryName(data.category_id, data.new_name);
      break;
    case "MOVE_CATEGORY":
      setWindowViewAtCurrentCategory("move", data.category_id, data.direction);
      break;
    case "DELETE_CATEGORY":
      setWindowViewAtCurrentCategory("delete", data.category_id, null);
      break;
    default:
      console.log("Unhandled event:", data);
  }
};

socket.onAny((eventName, data) => {
  if (eventName === "connect") return;
  if (!data.request_updates) {
    console.log(`Received event: ${eventName}`, data);
    handleEvent(data);
    localStorage.setItem("lastTimestamp", toLocalISOString());
  } else {
    console.log(`Received update: ${eventName}`, data);
    handleEvent(data);
    localStorage.setItem("lastTimestamp", toLocalISOString());
  }
});
