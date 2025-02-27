function updateViewCategoryName(categoryId, newName) {
  const categoryLink = document.querySelector(`a[href^="#collapseCategory${categoryId}"]`);

  if (categoryLink) {
    categoryLink.textContent = newName;

    const currentCollapseId = categoryLink.getAttribute("href").replace("#", "");
    const newCollapseId = `collapseCategory${categoryId}`;

    if (currentCollapseId !== newCollapseId) {
      categoryLink.setAttribute("href", `#${newCollapseId}`);
      categoryLink.setAttribute("aria-controls", newCollapseId);
    }
    removeLocationHash();
  }
}

function updateViewCategoryMoveButtons() {
  const categories = document.querySelectorAll(".category-block");

  categories.forEach((category, index) => {
    const upButton = category.querySelector('[onclick*="up"]');
    const downButton = category.querySelector('[onclick*="down"]');

    // Update button state
    upButton.disabled = index === 0;
    downButton.disabled = index === categories.length - 1;

    // Update Bootstrap attributes
    upButton.classList.toggle("disabled", index === 0);
    downButton.classList.toggle("disabled", index === categories.length - 1);
  });
}

function setWindowViewAtCurrentCategory(action, categoryId) {
  let currentElement;
  let previousElement;
  let nextElement;

  switch (action) {
    case "move":
      sessionStorage.setItem("scrollToCategoryId", categoryId);
      location.reload();
      break;
    case "delete":
      currentElement = document.querySelector("#category" + categoryId + ".category-block");
      previousElement = currentElement.previousElementSibling;
      nextElement = currentElement.nextElementSibling;

      if (previousElement.classList.contains("category-block")) {
        let prevCategoryId = previousElement.id.replace("category", "");
        sessionStorage.setItem("scrollToCategoryId", prevCategoryId);
        currentElement.remove();
        scrollToView();
      } else if (nextElement == null) {
        sessionStorage.setItem("removeLocationHash", true);
        currentElement.remove();
        scrollToView();
      } else {
        let nextCategoryId = nextElement.id.replace("category", "");
        sessionStorage.setItem("scrollToCategoryId", nextCategoryId);
        currentElement.remove();
        scrollToView();
      }
      break;
  }
}

// Scrooling view to last/previous/next category after page reload
// and/or remove location hash from address
document.addEventListener("DOMContentLoaded", scrollToView);

function scrollToView() {
  const categoryId = sessionStorage.getItem("scrollToCategoryId");
  const remLocationHash = sessionStorage.getItem("removeLocationHash");
  if (categoryId) {
    const element = document.getElementById("category" + categoryId);
    if (element) {
      element.scrollIntoView(true);

      // Remove id from sessionStorage after scroll
      sessionStorage.removeItem("scrollToCategoryId");
      removeLocationHash();
    }
  }

  if (remLocationHash) {
    sessionStorage.removeItem("removeLocationHash");
    removeLocationHash();
  }
}
