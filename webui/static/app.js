// Auto-submit the filter form as the user searches/filters, instead of
// requiring a manual "Search" click. Text input is debounced; discrete
// controls (checkboxes, dates) submit immediately on change.
document.addEventListener("DOMContentLoaded", () => {
  const form = document.querySelector("form.filters");
  if (!form) return;

  let debounceTimer;
  const qInput = form.querySelector('input[name="q"]');
  if (qInput) {
    qInput.addEventListener("input", () => {
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(() => form.requestSubmit(), 400);
    });
  }

  form.querySelectorAll('input[type="checkbox"], input[type="date"]').forEach((el) => {
    el.addEventListener("change", () => {
      clearTimeout(debounceTimer);
      form.requestSubmit();
    });
  });

  // Searchable multi-select dropdown for regulators.
  const regSelect = document.querySelector(".reg-select");
  if (regSelect) {
    const toggle = regSelect.querySelector(".reg-select-toggle");
    const panel = regSelect.querySelector(".reg-select-panel");
    const filterInput = regSelect.querySelector(".reg-filter");
    const options = regSelect.querySelectorAll(".reg-select-list label.reg");

    toggle.addEventListener("click", (e) => {
      e.stopPropagation();
      panel.hidden = !panel.hidden;
      if (!panel.hidden) filterInput.focus();
    });

    document.addEventListener("click", (e) => {
      if (!regSelect.contains(e.target)) panel.hidden = true;
    });

    // Don't let clicks inside the panel (typing, checking boxes) bubble up
    // and immediately re-close it via the outside-click handler above.
    panel.addEventListener("click", (e) => e.stopPropagation());

    if (filterInput) {
      filterInput.addEventListener("input", () => {
        const term = filterInput.value.trim().toLowerCase();
        options.forEach((label) => {
          const matches = !term || label.dataset.search.includes(term);
          label.hidden = !matches;
        });
      });
    }
  }
});
