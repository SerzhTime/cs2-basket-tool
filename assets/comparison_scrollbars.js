(function () {
  const win = window.parent;
  const doc = win.document;

  function setup(shell) {
    if (shell.dataset.overlayScrollReady === "1") return;
    const scroller = shell.querySelector(".comparison-table-scroll");
    const vertical = shell.querySelector(".comparison-overlay-scrollbar.vertical");
    const horizontal = shell.querySelector(".comparison-overlay-scrollbar.horizontal");
    if (!scroller || !vertical || !horizontal) return;
    shell.dataset.overlayScrollReady = "1";
    const vThumb = vertical.firstElementChild;
    const hThumb = horizontal.firstElementChild;

    function update() {
      const vOverflow = scroller.scrollHeight - scroller.clientHeight;
      const hOverflow = scroller.scrollWidth - scroller.clientWidth;
      vertical.classList.toggle("active", vOverflow > 1);
      horizontal.classList.toggle("active", hOverflow > 1);
      if (vOverflow > 1) {
        const track = vertical.clientHeight;
        const size = Math.max(28, track * scroller.clientHeight / scroller.scrollHeight);
        vThumb.style.height = size + "px";
        vThumb.style.transform = `translateY(${(track - size) * scroller.scrollTop / vOverflow}px)`;
      }
      if (hOverflow > 1) {
        const track = horizontal.clientWidth;
        const size = Math.max(28, track * scroller.clientWidth / scroller.scrollWidth);
        hThumb.style.width = size + "px";
        hThumb.style.transform = `translateX(${(track - size) * scroller.scrollLeft / hOverflow}px)`;
      }
    }

    function makeDraggable(track, thumb, axis) {
      thumb.addEventListener("pointerdown", (event) => {
        event.preventDefault();
        event.stopPropagation();
        track.classList.add("dragging");
        thumb.setPointerCapture(event.pointerId);
        const startPointer = axis === "x" ? event.clientX : event.clientY;
        const startScroll = axis === "x" ? scroller.scrollLeft : scroller.scrollTop;
        const trackSize = axis === "x" ? track.clientWidth : track.clientHeight;
        const thumbSize = axis === "x" ? thumb.offsetWidth : thumb.offsetHeight;
        const scrollRange = axis === "x"
          ? scroller.scrollWidth - scroller.clientWidth
          : scroller.scrollHeight - scroller.clientHeight;
        const move = (moveEvent) => {
          const pointer = axis === "x" ? moveEvent.clientX : moveEvent.clientY;
          const next = startScroll + (pointer - startPointer) * scrollRange / Math.max(1, trackSize - thumbSize);
          if (axis === "x") scroller.scrollLeft = next;
          else scroller.scrollTop = next;
        };
        const finish = () => {
          track.classList.remove("dragging");
          win.removeEventListener("pointermove", move);
          win.removeEventListener("pointerup", finish);
          win.removeEventListener("pointercancel", finish);
        };
        win.addEventListener("pointermove", move);
        win.addEventListener("pointerup", finish);
        win.addEventListener("pointercancel", finish);
      });
    }

    scroller.addEventListener("scroll", update, { passive: true });
    makeDraggable(vertical, vThumb, "y");
    makeDraggable(horizontal, hThumb, "x");
    if (win.ResizeObserver) new win.ResizeObserver(update).observe(scroller);
    update();
  }

  doc.querySelectorAll(".comparison-scroll-shell").forEach(setup);
})();
