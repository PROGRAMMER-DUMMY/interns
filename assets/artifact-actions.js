(function () {
  let activePopover = null;
  let activeTrigger = null;

  async function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      try {
        await navigator.clipboard.writeText(text);
        return;
      } catch (_error) {
        // Fall through to the legacy selection copy path when browser
        // permissions expose Clipboard API but deny the write.
      }
    }
    const input = document.createElement("textarea");
    input.value = text;
    input.setAttribute("readonly", "readonly");
    input.style.position = "fixed";
    input.style.opacity = "0";
    document.body.appendChild(input);
    input.select();
    document.execCommand("copy");
    document.body.removeChild(input);
  }

  function closePopover() {
    if (activePopover) {
      activePopover.remove();
    }
    if (activeTrigger) {
      activeTrigger.setAttribute("aria-expanded", "false");
    }
    activePopover = null;
    activeTrigger = null;
  }

  function placePopover(trigger, popover) {
    const rect = trigger.getBoundingClientRect();
    const width = Math.min(340, Math.max(260, rect.width));
    popover.style.width = `${width}px`;
    popover.style.left = `${Math.min(rect.left, window.innerWidth - width - 12)}px`;
    popover.style.top = `${Math.min(rect.bottom + 8, window.innerHeight - 128)}px`;
  }

  function showPathPopover(trigger) {
    const path = trigger.getAttribute("data-copy-path") || "";
    const href = trigger.getAttribute("data-open-href") || trigger.href || "";
    if (!path) {
      return;
    }
    if (activeTrigger === trigger) {
      closePopover();
      return;
    }
    closePopover();
    const popover = document.createElement("div");
    popover.className = "assistant-artifact-path-popover";
    popover.setAttribute("role", "dialog");
    popover.setAttribute("aria-label", "File path actions");
    popover.innerHTML = `
      <div class="assistant-artifact-path-popover-label">File path</div>
      <div class="assistant-artifact-path-popover-value"></div>
      <div class="assistant-artifact-path-popover-actions">
        <button type="button" class="assistant-artifact-path-popover-copy" data-copy-action="copy-path">Copy path</button>
        <a class="assistant-artifact-path-popover-open">Open file</a>
      </div>
    `;
    popover.querySelector(".assistant-artifact-path-popover-value").textContent = path;
    const openLink = popover.querySelector(".assistant-artifact-path-popover-open");
    openLink.href = href || "#";
    document.body.appendChild(popover);
    placePopover(trigger, popover);
    trigger.setAttribute("aria-expanded", "true");
    activePopover = popover;
    activeTrigger = trigger;
  }

  document.addEventListener("click", async (event) => {
    const pathTrigger = event.target.closest(".assistant-artifact-path-link[data-copy-path]");
    if (pathTrigger) {
      event.preventDefault();
      showPathPopover(pathTrigger);
      return;
    }

    const copyButton = event.target.closest("[data-copy-action='copy-path']");
    if (!copyButton || !activeTrigger) {
      if (!event.target.closest(".assistant-artifact-path-popover")) {
        closePopover();
      }
      return;
    }
    event.preventDefault();
    const path = activeTrigger.getAttribute("data-copy-path") || "";
    const original = copyButton.textContent;
    try {
      await copyText(path);
      copyButton.textContent = "Copied";
      window.setTimeout(() => {
        copyButton.textContent = original || "Copy path";
      }, 1200);
    } catch (_error) {
      copyButton.textContent = "Failed";
      window.setTimeout(() => {
        copyButton.textContent = original || "Copy path";
      }, 1200);
    }
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closePopover();
    }
  });

  window.addEventListener("resize", closePopover);
  window.addEventListener("scroll", closePopover, true);
})();
