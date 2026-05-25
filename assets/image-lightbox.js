(function () {
  function ensureLightbox() {
    let lightbox = document.querySelector(".assistant-image-lightbox");
    if (lightbox) {
      return lightbox;
    }

    lightbox = document.createElement("div");
    lightbox.className = "assistant-image-lightbox";
    lightbox.setAttribute("role", "dialog");
    lightbox.setAttribute("aria-modal", "true");
    lightbox.setAttribute("aria-label", "Image preview");
    lightbox.innerHTML = [
      '<div class="assistant-image-lightbox-frame">',
      '<button class="assistant-image-lightbox-close" type="button" aria-label="Close image preview">x</button>',
      '<img class="assistant-image-lightbox-img" alt="">',
      "</div>",
    ].join("");
    document.body.appendChild(lightbox);

    lightbox.addEventListener("click", (event) => {
      if (
        event.target === lightbox ||
        event.target.classList.contains("assistant-image-lightbox-close")
      ) {
        closeLightbox();
      }
    });
    return lightbox;
  }

  function closeLightbox() {
    const lightbox = document.querySelector(".assistant-image-lightbox");
    if (!lightbox) {
      return;
    }
    lightbox.classList.remove("is-open");
    document.body.classList.remove("assistant-image-lightbox-open");
  }

  function openLightbox(image) {
    const lightbox = ensureLightbox();
    const lightboxImage = lightbox.querySelector(".assistant-image-lightbox-img");
    lightboxImage.src = image.currentSrc || image.src;
    lightboxImage.alt = image.alt || "Image preview";
    lightbox.classList.add("is-open");
    document.body.classList.add("assistant-image-lightbox-open");
  }

  document.addEventListener("click", (event) => {
    const image = event.target.closest(".assistant-image-preview");
    if (!image) {
      return;
    }
    event.preventDefault();
    openLightbox(image);
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closeLightbox();
    }
  });
})();
