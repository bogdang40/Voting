document.addEventListener("DOMContentLoaded", () => {
  const list = document.getElementById("candidate-list");
  const addBtn = document.getElementById("btn-add-candidate");
  const form = document.getElementById("new-instance-form");
  const submitBtn = document.getElementById("btn-create-instance");

  if (!list || !addBtn || !form || !submitBtn) return;

  const renumberPlaceholders = () => {
    Array.from(list.querySelectorAll(".candidate-row")).forEach((row, idx) => {
      const input = row.querySelector("input[name='candidate_name']");
      if (input && !input.value.trim()) {
        input.placeholder = `Nume candidat ${idx + 1}`;
      }
    });
  };

  const refreshRemoveButtons = () => {
    const rows = Array.from(list.querySelectorAll(".candidate-row"));
    rows.forEach((row) => {
      const remove = row.querySelector(".btn-remove");
      if (remove) remove.style.display = rows.length > 1 ? "inline-flex" : "none";
    });
    renumberPlaceholders();
  };

  addBtn.addEventListener("click", () => {
    const count = list.querySelectorAll(".candidate-row").length + 1;
    const row = document.createElement("div");
    row.className = "candidate-row";
    row.innerHTML = `
      <input class="input" type="text" name="candidate_name" autocomplete="off" placeholder="Nume candidat ${count}">
      <button type="button" class="btn btn-danger btn-remove">Sterge</button>
    `;
    list.appendChild(row);
    refreshRemoveButtons();
    row.querySelector("input")?.focus();
  });

  list.addEventListener("click", (ev) => {
    const target = ev.target;
    if (!(target instanceof HTMLElement)) return;
    if (!target.classList.contains("btn-remove")) return;

    const rows = list.querySelectorAll(".candidate-row");
    if (rows.length <= 1) return;

    const row = target.closest(".candidate-row");
    if (row) row.remove();
    refreshRemoveButtons();
  });

  form.addEventListener("submit", () => {
    submitBtn.disabled = true;
    submitBtn.textContent = "Se creeaza...";
  });

  refreshRemoveButtons();
});
