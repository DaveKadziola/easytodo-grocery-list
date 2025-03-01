const socket = io("http://192.168.100.18:8100", {
  reconnection: true,
  transports: ["websocket"],
  upgrade: false,
});

socket.on("connect", () => {
  console.log("Connected to WebSocket");
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
  console.log(`Received event: ${eventName}`, data);
  handleEvent(data);
});
