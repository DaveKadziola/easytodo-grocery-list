// Scrooling view to last/previous/next category after page reload
document.addEventListener("DOMContentLoaded", function () {
  const categoryId = sessionStorage.getItem("scrollToCategoryId");
  if (categoryId) {
    const element = document.getElementById("category" + categoryId);
    if (element) {
      element.scrollIntoView(true);

      // Remove id from sessionStorage after scroll
      sessionStorage.removeItem("scrollToCategoryId");
    }
  }
});
