document.addEventListener("DOMContentLoaded", () => {
  const lightbox = document.getElementById("lightbox");
  const lbImg = document.getElementById("lb-img");
  const lbLabel = document.getElementById("lb-label");
  const lbClose = document.getElementById("lb-close");
  const lbPrev = document.getElementById("lb-prev");
  const lbNext = document.getElementById("lb-next");
  const thumbs = Array.from(document.querySelectorAll(".thumb[data-src]"));

  let current = 0;
  let refreshTimer = null;

  const startRefresh = () => {
    refreshTimer = window.setTimeout(() => window.location.reload(), 15000);
  };

  const stopRefresh = () => {
    if (refreshTimer) {
      window.clearTimeout(refreshTimer);
      refreshTimer = null;
    }
  };

  const showAt = (idx) => {
    const item = thumbs[idx];
    if (!item || !lbImg || !lbLabel) return;
    lbImg.src = item.dataset.src || "";
    lbLabel.textContent = item.dataset.label || "";
    if (lbPrev) lbPrev.style.visibility = idx > 0 ? "visible" : "hidden";
    if (lbNext) lbNext.style.visibility = idx < thumbs.length - 1 ? "visible" : "hidden";
  };

  const openAt = (idx) => {
    if (!lightbox) return;
    current = idx;
    showAt(current);
    lightbox.classList.add("open");
    stopRefresh();
  };

  const closeLightbox = () => {
    if (!lightbox) return;
    lightbox.classList.remove("open");
    startRefresh();
  };

  thumbs.forEach((thumb, idx) => {
    thumb.addEventListener("click", () => openAt(idx));
  });

  lbClose?.addEventListener("click", closeLightbox);
  lbPrev?.addEventListener("click", (ev) => {
    ev.stopPropagation();
    current = Math.max(0, current - 1);
    showAt(current);
  });
  lbNext?.addEventListener("click", (ev) => {
    ev.stopPropagation();
    current = Math.min(thumbs.length - 1, current + 1);
    showAt(current);
  });

  lightbox?.addEventListener("click", (ev) => {
    if (ev.target === lightbox) closeLightbox();
  });

  document.addEventListener("keydown", (ev) => {
    if (!lightbox?.classList.contains("open")) return;
    if (ev.key === "Escape") closeLightbox();
    if (ev.key === "ArrowLeft") {
      current = Math.max(0, current - 1);
      showAt(current);
    }
    if (ev.key === "ArrowRight") {
      current = Math.min(thumbs.length - 1, current + 1);
      showAt(current);
    }
  });

  startRefresh();
});
