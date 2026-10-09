// Incident report: local times, auto-refresh on picker change, print.
"use strict";

(() => {
  document.querySelectorAll("time[datetime]").forEach((node) => {
    const at = new Date(node.getAttribute("datetime"));
    if (!Number.isNaN(at.getTime())) node.textContent = at.toLocaleTimeString();
  });
  const form = document.getElementById("incident-form");
  if (form) {
    form.querySelectorAll("select").forEach((select) =>
      select.addEventListener("change", () => form.submit()));
  }
  const print = document.getElementById("print-report");
  if (print) print.addEventListener("click", () => window.print());
})();
