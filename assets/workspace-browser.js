(function () {
  const SIDEBAR_KEY = "autoresearch.workspace.sidebarWidth";
  const DETAIL_KEY = "autoresearch.workspace.detailWidth";
  const LEFT_COLLAPSED_KEY = "autoresearch.workspace.leftCollapsed";
  const RIGHT_COLLAPSED_KEY = "autoresearch.workspace.rightCollapsed";
  const SIDEBAR_MIN = 180;
  const SIDEBAR_MAX = 420;
  const DETAIL_MIN = 340;
  const DETAIL_MAX = 560;

  function clamp(value, min, max) {
    return Math.max(min, Math.min(max, value));
  }

  function readStorage(key) {
    try {
      return window.localStorage.getItem(key);
    } catch (_error) {
      return null;
    }
  }

  function writeStorage(key, value) {
    try {
      window.localStorage.setItem(key, String(value));
    } catch (_error) {
      // localStorage can be unavailable in restricted browser contexts.
    }
  }

  function applyWidths(shell) {
    const sidebarWidth = clamp(Number(readStorage(SIDEBAR_KEY)) || 240, SIDEBAR_MIN, SIDEBAR_MAX);
    const detailWidth = clamp(Number(readStorage(DETAIL_KEY)) || 400, DETAIL_MIN, DETAIL_MAX);
    shell.style.setProperty("--ws-sidebar-width", `${sidebarWidth}px`);
    shell.style.setProperty("--ws-detail-width", `${detailWidth}px`);
  }

  function applyCollapsedState(shell) {
    const leftCollapsed = readStorage(LEFT_COLLAPSED_KEY) === "true";
    const rightCollapsed = readStorage(RIGHT_COLLAPSED_KEY) === "true";
    shell.classList.toggle("is-left-collapsed", leftCollapsed);
    shell.classList.toggle("is-right-collapsed", rightCollapsed);
    syncButtons(shell);
  }

  function syncButtons(shell) {
    const leftCollapsed = shell.classList.contains("is-left-collapsed");
    const rightCollapsed = shell.classList.contains("is-right-collapsed");
    shell.querySelectorAll('[data-ws-toggle="left"]').forEach((button) => {
      button.classList.toggle("is-collapsed", leftCollapsed);
      button.textContent = leftCollapsed ? "+" : "-";
    });
    shell.querySelectorAll('[data-ws-toggle="right"]').forEach((button) => {
      button.classList.toggle("is-collapsed", rightCollapsed);
      button.textContent = rightCollapsed ? "<" : ">";
      button.title = rightCollapsed ? "Expand inspector" : "Collapse inspector";
    });
  }

  function setCollapsed(shell, side, nextValue) {
    const className = side === "left" ? "is-left-collapsed" : "is-right-collapsed";
    const storageKey = side === "left" ? LEFT_COLLAPSED_KEY : RIGHT_COLLAPSED_KEY;
    shell.classList.toggle(className, nextValue);
    writeStorage(storageKey, nextValue ? "true" : "false");
    syncButtons(shell);
  }

  function bindToggleButtons(shell) {
    shell.querySelectorAll("[data-ws-toggle]").forEach((button) => {
      if (button.dataset.bound === "true") {
        return;
      }
      button.dataset.bound = "true";
      button.addEventListener("click", () => {
        const side = button.getAttribute("data-ws-toggle");
        const className = side === "left" ? "is-left-collapsed" : "is-right-collapsed";
        setCollapsed(shell, side, !shell.classList.contains(className));
      });
    });
  }

  function bindResizer(shell, handle, side) {
    if (!handle || handle.dataset.bound === "true") {
      return;
    }
    handle.dataset.bound = "true";
    handle.addEventListener("mousedown", (event) => {
      if (event.target.closest("[data-ws-toggle]")) {
        return;
      }
      event.preventDefault();
      handle.classList.add("is-dragging");
      document.body.classList.add("workspace-browser-resizing");
      setCollapsed(shell, side, false);
      const rect = shell.getBoundingClientRect();

      function onMove(moveEvent) {
        if (side === "left") {
          const nextWidth = clamp(moveEvent.clientX - rect.left, SIDEBAR_MIN, SIDEBAR_MAX);
          shell.style.setProperty("--ws-sidebar-width", `${nextWidth}px`);
          writeStorage(SIDEBAR_KEY, nextWidth);
        } else {
          const nextWidth = clamp(rect.right - moveEvent.clientX, DETAIL_MIN, DETAIL_MAX);
          shell.style.setProperty("--ws-detail-width", `${nextWidth}px`);
          writeStorage(DETAIL_KEY, nextWidth);
        }
      }

      function onUp() {
        handle.classList.remove("is-dragging");
        document.body.classList.remove("workspace-browser-resizing");
        window.removeEventListener("mousemove", onMove);
        window.removeEventListener("mouseup", onUp);
      }

      window.addEventListener("mousemove", onMove);
      window.addEventListener("mouseup", onUp);
    });
  }

  function bindShell(shell) {
    if (!shell || shell.dataset.workspaceBrowserBound === "true") {
      return;
    }
    shell.dataset.workspaceBrowserBound = "true";
    applyWidths(shell);
    applyCollapsedState(shell);
    bindToggleButtons(shell);
    bindResizer(shell, shell.querySelector('.workspace-browser-resizer[data-side="left"]'), "left");
    bindResizer(shell, shell.querySelector('.workspace-browser-resizer[data-side="right"]'), "right");
  }

  function bindAll() {
    document.querySelectorAll(".workspace-browser-shell").forEach(bindShell);
  }

  document.addEventListener("DOMContentLoaded", bindAll);
  const observer = new MutationObserver(bindAll);
  observer.observe(document.documentElement, { childList: true, subtree: true });
})();
