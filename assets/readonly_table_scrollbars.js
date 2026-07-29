(function () {
  const win = window.parent;
  const doc = win.document;
  const selector = [
    ".st-key-history_timestamp_table",
    ".st-key-update_run_log",
    ".st-key-last_update_item_status",
    ".st-key-adapter_status",
    ".st-key-marketplace_coverage",
    ".st-key-performance_diagnostics"
  ].join(",");

  function setup(host) {
    const scroller = host.querySelector('[data-testid="stTable"] > div');
    if (!scroller) return;
    if (host.dataset.readonlyOverlayReady === "true" && host._readonlyScroller === scroller) {
      host._readonlyOverlayUpdate?.();
      return;
    }

    host.querySelectorAll(":scope > .cs2dt-readonly-scrollbar").forEach((bar) => bar.remove());
    host.classList.add("cs2dt-readonly-overlay-host");
    scroller.classList.add("cs2dt-readonly-overlay-scroller");

    const vertical = doc.createElement("div");
    vertical.className = "cs2dt-readonly-scrollbar vertical";
    vertical.innerHTML = '<div class="thumb"></div>';
    const horizontal = doc.createElement("div");
    horizontal.className = "cs2dt-readonly-scrollbar horizontal";
    horizontal.innerHTML = '<div class="thumb"></div>';
    host.append(vertical, horizontal);

    const verticalThumb = vertical.firstElementChild;
    const horizontalThumb = horizontal.firstElementChild;

    function update() {
      const verticalOverflow = scroller.scrollHeight - scroller.clientHeight;
      const horizontalOverflow = scroller.scrollWidth - scroller.clientWidth;
      vertical.style.display = verticalOverflow > 1 ? "block" : "none";
      horizontal.style.display = horizontalOverflow > 1 ? "block" : "none";

      if (verticalOverflow > 1) {
        const trackSize = vertical.clientHeight;
        const thumbSize = Math.max(28, trackSize * scroller.clientHeight / scroller.scrollHeight);
        verticalThumb.style.height = `${thumbSize}px`;
        verticalThumb.style.transform = `translateY(${(trackSize - thumbSize) * scroller.scrollTop / verticalOverflow}px)`;
      }
      if (horizontalOverflow > 1) {
        const trackSize = horizontal.clientWidth;
        const thumbSize = Math.max(28, trackSize * scroller.clientWidth / scroller.scrollWidth);
        horizontalThumb.style.width = `${thumbSize}px`;
        horizontalThumb.style.transform = `translateX(${(trackSize - thumbSize) * scroller.scrollLeft / horizontalOverflow}px)`;
      }
    }

    function bindDrag(track, thumb, axis) {
      thumb.addEventListener("pointerdown", (event) => {
        event.preventDefault();
        event.stopPropagation();
        track.classList.add("dragging");
        const startPointer = axis === "x" ? event.clientX : event.clientY;
        const startScroll = axis === "x" ? scroller.scrollLeft : scroller.scrollTop;
        const trackSize = axis === "x" ? track.clientWidth : track.clientHeight;
        const thumbSize = axis === "x" ? thumb.offsetWidth : thumb.offsetHeight;
        const scrollRange = axis === "x"
          ? scroller.scrollWidth - scroller.clientWidth
          : scroller.scrollHeight - scroller.clientHeight;

        function move(moveEvent) {
          const pointer = axis === "x" ? moveEvent.clientX : moveEvent.clientY;
          const next = startScroll + (pointer - startPointer) * scrollRange / Math.max(1, trackSize - thumbSize);
          if (axis === "x") scroller.scrollLeft = next;
          else scroller.scrollTop = next;
        }
        function finish() {
          track.classList.remove("dragging");
          win.removeEventListener("pointermove", move);
          win.removeEventListener("pointerup", finish);
          win.removeEventListener("pointercancel", finish);
        }
        win.addEventListener("pointermove", move);
        win.addEventListener("pointerup", finish);
        win.addEventListener("pointercancel", finish);
      });
    }

    scroller.addEventListener("scroll", update, { passive: true });
    bindDrag(vertical, verticalThumb, "y");
    bindDrag(horizontal, horizontalThumb, "x");
    if (win.ResizeObserver) new win.ResizeObserver(update).observe(scroller);
    host.dataset.readonlyOverlayReady = "true";
    host._readonlyScroller = scroller;
    host._readonlyOverlayUpdate = update;
    update();
  }

  function setupAll() {
    doc.querySelectorAll(selector).forEach(setup);
  }

  function startObserver() {
    if (!doc.body) {
      win.setTimeout(startObserver, 0);
      return;
    }
    setupAll();
    win.__cs2dtReadonlyTableObserver?.disconnect();
    const observer = new win.MutationObserver(setupAll);
    observer.observe(doc.body, { childList: true, subtree: true });
    win.__cs2dtReadonlyTableObserver = observer;
  }

  startObserver();
})();
