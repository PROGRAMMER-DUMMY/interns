(function () {
  const STORAGE_KEY = "autoresearch.sidebarWidth";
  const MIN_WIDTH = 160;
  const MAX_WIDTH = 360;

  function clamp(value) {
    return Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, value));
  }

  function setWidth(value) {
    const width = clamp(Number(value) || 200);
    document.documentElement.style.setProperty("--sidebar-width", `${width}px`);
    try {
      window.localStorage.setItem(STORAGE_KEY, String(width));
    } catch (_error) {
      // localStorage may be unavailable in private or locked-down browser contexts.
    }
  }

  function storedWidth() {
    try {
      return window.localStorage.getItem(STORAGE_KEY);
    } catch (_error) {
      return null;
    }
  }

  function bindResizer() {
    const handle = document.getElementById("sidebar-resizer");
    if (!handle || handle.dataset.bound === "true") {
      return;
    }
    handle.dataset.bound = "true";

    handle.addEventListener("mousedown", (event) => {
      event.preventDefault();
      handle.classList.add("is-dragging");
      document.body.classList.add("sidebar-resizing");

      function onMove(moveEvent) {
        setWidth(moveEvent.clientX);
      }

      function onUp() {
        handle.classList.remove("is-dragging");
        document.body.classList.remove("sidebar-resizing");
        window.removeEventListener("mousemove", onMove);
        window.removeEventListener("mouseup", onUp);
      }

      window.addEventListener("mousemove", onMove);
      window.addEventListener("mouseup", onUp);
    });
  }

  setWidth(storedWidth() || 200);
  document.addEventListener("DOMContentLoaded", bindResizer);

  const observer = new MutationObserver(bindResizer);
  observer.observe(document.documentElement, { childList: true, subtree: true });
})();
