// Auto-submit the filter form as the user searches/filters, instead of
// requiring a manual "Search" click. Text input is debounced; discrete
// controls (checkboxes, dates) submit immediately on change.
const REG_PANEL_FLAG = "caspRadarRegPanelOpen";
const SEARCH_FOCUS_FLAG = "caspRadarSearchFocus";

document.addEventListener("DOMContentLoaded", () => {
  const form = document.querySelector("form.filters");
  if (!form) return;

  let debounceTimer;
  const qInput = form.querySelector('input[name="q"]');
  if (qInput) {
    // The debounced submit does a full page navigation, which drops focus -
    // without restoring it, every auto-search forces a re-click into the
    // box to keep typing. One-shot flag (same pattern as REG_PANEL_FLAG):
    // set right before submitting, consumed on the next page load only.
    if (sessionStorage.getItem(SEARCH_FOCUS_FLAG)) {
      sessionStorage.removeItem(SEARCH_FOCUS_FLAG);
      qInput.focus();
      const end = qInput.value.length;
      qInput.setSelectionRange(end, end);
    }

    qInput.addEventListener("input", () => {
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(() => {
        sessionStorage.setItem(SEARCH_FOCUS_FLAG, "1");
        form.requestSubmit();
      }, 400);
    });
  }

  form.querySelectorAll('input[type="date"], input[type="checkbox"]:not(.reg-select-list input)').forEach((el) => {
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
    const selectAllBtn = regSelect.querySelector(".reg-select-all");

    // Reopen the panel after a regulator checkbox submits the page - but only
    // then, not on every subsequent navigation that happens to still carry
    // regulator_id in the URL (e.g. clicking a month link). One-shot flag,
    // cleared immediately after use, rather than inferring "should be open"
    // from whether any regulator is currently selected.
    if (sessionStorage.getItem(REG_PANEL_FLAG)) {
      panel.hidden = false;
      sessionStorage.removeItem(REG_PANEL_FLAG);
    }

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

    regSelect.querySelectorAll('.reg-select-list input[type="checkbox"]').forEach((el) => {
      el.addEventListener("change", () => {
        clearTimeout(debounceTimer);
        sessionStorage.setItem(REG_PANEL_FLAG, "1");
        form.requestSubmit();
      });
    });

    if (filterInput) {
      filterInput.addEventListener("input", () => {
        const term = filterInput.value.trim().toLowerCase();
        options.forEach((label) => {
          const matches = !term || label.dataset.search.includes(term);
          label.hidden = !matches;
        });
      });
    }

    if (selectAllBtn) {
      selectAllBtn.addEventListener("click", () => {
        let changed = false;
        options.forEach((label) => {
          if (label.hidden) return;
          const box = label.querySelector('input[type="checkbox"]');
          if (box && !box.checked) {
            box.checked = true;
            changed = true;
          }
        });
        if (changed) {
          sessionStorage.setItem(REG_PANEL_FLAG, "1");
          form.requestSubmit();
        }
      });
    }
  }
});
