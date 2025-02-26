// Remove location hash from address
function removeLocationHash() {
  if (window.history && window.history.pushState) {
    window.history.pushState("", document.title, window.location.pathname + window.location.search);
  }
}
