document.addEventListener("DOMContentLoaded", () => {
  const form = document.getElementById("review-correct-form");
  if (!form) return;

  const selects = Array.from(form.querySelectorAll(".vote-select[data-original]"));
  const reasonWrap = document.getElementById("review-reason-wrap");
  const reasonInput = document.getElementById("review-reason");
  const submitBtn = document.getElementById("btn-submit-correction");
  const changedCount = document.getElementById("changed-count");

  const normalize = (value) => String(value || "").trim().toUpperCase();

  const syncState = () => {
    let changes = 0;

    selects.forEach((select) => {
      const original = normalize(select.dataset.original);
      const current = normalize(select.value);
      const row = select.closest("tr");
      const changed = original !== current;

      if (row) row.classList.toggle("vote-changed", changed);
      if (changed) changes += 1;
    });

    if (changedCount) changedCount.textContent = String(changes);

    const hasChanges = changes > 0;
    if (reasonWrap) reasonWrap.hidden = !hasChanges;
    if (reasonInput) reasonInput.required = hasChanges;
    if (submitBtn) submitBtn.disabled = !hasChanges;
  };

  selects.forEach((select) => {
    select.addEventListener("change", syncState);
  });

  syncState();
});
