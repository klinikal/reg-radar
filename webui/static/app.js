// Auto-submit the filter form as the user searches/filters, instead of
// requiring a manual "Search" click. Text input is debounced; discrete
// controls (checkboxes, dates) submit immediately on change.
const PANEL_OPEN_FLAG = "caspRadarPanelOpen";
const SEARCH_FOCUS_FLAG = "caspRadarSearchFocus";

document.addEventListener("DOMContentLoaded", () => {
  const themeToggle = document.getElementById("theme-toggle");
  if (themeToggle) {
    const isDark = () => document.documentElement.dataset.theme
      ? document.documentElement.dataset.theme === "dark"
      : matchMedia("(prefers-color-scheme: dark)").matches;
    const render = () => { themeToggle.textContent = isDark() ? "☀️" : "🌙"; };
    render();
    themeToggle.addEventListener("click", () => {
      document.documentElement.dataset.theme = isDark() ? "light" : "dark";
      try { localStorage.setItem("theme", document.documentElement.dataset.theme); } catch (e) {}
      render();
    });
  }

  const form = document.querySelector("form.filters");
  if (!form) return;

  let debounceTimer;
  const qInput = form.querySelector('input[name="q"]');
  if (qInput) {
    // The debounced submit does a full page navigation, which drops focus -
    // without restoring it, every auto-search forces a re-click into the
    // box to keep typing. One-shot flag (same pattern as PANEL_OPEN_FLAG):
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

  form.querySelectorAll('input[type="date"], input[type="checkbox"]:not(.reg-select-list input), select').forEach((el) => {
    el.addEventListener("change", () => {
      clearTimeout(debounceTimer);
      form.requestSubmit();
    });
  });

  // Multi-select dropdowns for regulators and jurisdictions.
  document.querySelectorAll(".reg-select").forEach((regSelect) => {
    const toggle = regSelect.querySelector(".reg-select-toggle");
    const panel = regSelect.querySelector(".reg-select-panel");
    const filterInput = regSelect.querySelector(".reg-filter");
    const options = regSelect.querySelectorAll(".reg-select-list label.reg");
    const selectAllBtn = regSelect.querySelector(".reg-select-all");
    const checkbox = regSelect.querySelector('.reg-select-list input[type="checkbox"]');
    const panelFlag = `${PANEL_OPEN_FLAG}:${checkbox?.name || "dropdown"}`;

    // Reopen the panel after one of its checkboxes submits the page - but only
    // then, not on every subsequent navigation that happens to still carry
    // regulator_id in the URL (e.g. clicking a month link). One-shot flag,
    // cleared immediately after use, rather than inferring "should be open"
    // from whether any regulator is currently selected.
    if (sessionStorage.getItem(panelFlag)) {
      panel.hidden = false;
      sessionStorage.removeItem(panelFlag);
    }

    toggle.addEventListener("click", (e) => {
      e.stopPropagation();
      panel.hidden = !panel.hidden;
      if (!panel.hidden && filterInput) filterInput.focus();
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
        sessionStorage.setItem(panelFlag, "1");
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
          sessionStorage.setItem(panelFlag, "1");
          form.requestSubmit();
        }
      });
    }
  });
});
