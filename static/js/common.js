document.addEventListener("DOMContentLoaded", () => {
  /* ── Confirm dialogs ── */
  document.querySelectorAll("[data-confirm]").forEach((el) => {
    el.addEventListener("click", (ev) => {
      const question = el.getAttribute("data-confirm") || "Sunteti sigur?";
      if (!window.confirm(question)) {
        ev.preventDefault();
      }
    });
  });

  /* ── Hamburger menu toggle ── */
  const hamburger = document.getElementById("hamburger-btn");
  const navCollapse = document.getElementById("nav-collapse");

  if (hamburger && navCollapse) {
    // On desktop, nav should always be visible
    const mq = window.matchMedia("(min-width: 741px)");

    function handleMQ(e) {
      if (e.matches) {
        navCollapse.classList.add("open");
      }
    }

    mq.addEventListener("change", handleMQ);
    handleMQ(mq);

    hamburger.addEventListener("click", () => {
      navCollapse.classList.toggle("open");
      hamburger.textContent = navCollapse.classList.contains("open") ? "✕" : "☰";
    });
  }

  /* ── Card entrance animations (Intersection Observer) ── */
  const animateEls = document.querySelectorAll("[data-animate]");
  if (animateEls.length > 0 && "IntersectionObserver" in window) {
    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry, i) => {
          if (entry.isIntersecting) {
            // Stagger the animation
            entry.target.style.animationDelay = `${i * 80}ms`;
            entry.target.style.opacity = "1";
            entry.target.classList.add("animate-in");
            observer.unobserve(entry.target);
          }
        });
      },
      { threshold: 0.08, rootMargin: "0px 0px -30px 0px" }
    );

    animateEls.forEach((el) => {
      el.style.opacity = "0";
      observer.observe(el);
    });

    // Safety net for browsers that miss initial intersection callbacks.
    window.setTimeout(() => {
      animateEls.forEach((el) => {
        if (!el.classList.contains("animate-in")) {
          el.style.opacity = "1";
          el.classList.add("animate-in");
          observer.unobserve(el);
        }
      });
    }, 900);
  } else {
    // Fallback: do not leave cards hidden when observer is unavailable.
    animateEls.forEach((el) => {
      el.style.opacity = "1";
      el.classList.add("animate-in");
    });
  }

  /* ── Smooth scroll for anchor links ── */
  document.querySelectorAll('a[href^="#"]').forEach((a) => {
    a.addEventListener("click", (e) => {
      const target = document.querySelector(a.getAttribute("href"));
      if (target) {
        e.preventDefault();
        target.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    });
  });
});
