// Admin console: feature switches and demo reset.
"use strict";

(() => {
  const { postJson } = window.HackNC;
  const result = document.getElementById("admin-result");

  function show(ok, text) {
    result.hidden = false;
    result.className = `alert ${ok ? "alert-ok" : "alert-deny"}`;
    result.textContent = text;
  }

  document.querySelectorAll("[data-flag]").forEach((input) => {
    input.addEventListener("change", async () => {
      input.disabled = true;
      try {
        const flags = await postJson("/api/admin/flags", { [input.dataset.flag]: input.checked });
        input.checked = !!flags[input.dataset.flag];
        show(true, `${input.dataset.flag} is now ${input.checked ? "on" : "off"}.`);
      } catch (err) {
        input.checked = !input.checked;
        show(false, err.message);
      } finally {
        input.disabled = false;
      }
    });
  });

  document.getElementById("prepare-finale").addEventListener("click", async (event) => {
    event.target.disabled = true;
    try {
      const body = await postJson("/api/admin/prepare-finale");
      Object.entries(body.flags || {}).forEach(([key, on]) => {
        const input = document.querySelector(`[data-flag="${CSS.escape(key)}"]`);
        if (input) input.checked = !!on;
      });
      const parts = Object.entries(body.results).map(([k, ok]) => `${ok ? "✓" : "✕"} ${k}`).join(" · ");
      show(body.ok, `Finale ready (AI model on, Wrong patient staged): ${parts}`);
    } catch (err) {
      show(false, err.message);
    } finally {
      event.target.disabled = false;
    }
  });

  document.getElementById("reset").addEventListener("click", async (event) => {
    event.target.disabled = true;
    try {
      const body = await postJson("/api/admin/reset");
      const parts = Object.entries(body.results).map(([k, ok]) => `${ok ? "✓" : "✕"} ${k}`).join(" · ");
      show(body.ok, `Demo reset: ${parts}`);
    } catch (err) {
      show(false, err.message);
    } finally {
      event.target.disabled = false;
    }
  });
})();
