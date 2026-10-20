// Remove location hash from address
function removeLocationHash() {
  if (window.history && window.history.pushState) {
    window.history.pushState("", document.title, window.location.pathname + window.location.search);
  }
}

function toLocalISOString(date = new Date()) {
  const pad = (n) => n.toString().padStart(2, "0");
  const tzOffset = -date.getTimezoneOffset();
  const diff = tzOffset >= 0 ? "+" : "-";
  const hours = pad(Math.floor(Math.abs(tzOffset) / 60));
  const minutes = pad(Math.abs(tzOffset) % 60);

  return (
    date.getFullYear() +
    "-" +
    pad(date.getMonth() + 1) +
    "-" +
    pad(date.getDate()) +
    " " + // Replace space with T in case of making ISO 8601 compliant
    pad(date.getHours()) +
    ":" +
    pad(date.getMinutes()) +
    ":" +
    pad(date.getSeconds()) +
    "." +
    date.getMilliseconds().toString().padStart(3, "0")
    //Now the diff (like +01:00) is not needed, only kept for future need
    //+ diff + hours + ":" + minutes
  );
}
