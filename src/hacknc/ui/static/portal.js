// Patient portal: send a message, follow my requests, choose who sees sensitive records.
"use strict";

(() => {
  const { el, getJson, postJson } = window.HackNC;
  const $ = (id) => document.getElementById(id);
  if (!$("portal-form")) return;

  const KIND = { medication_change: "Change to a medicine", refill: "Refill",
                 side_effect: "Side effect", question: "Question" };
  const STATUS = { open: ["Waiting for your doctor", "pill-pending"],
                   accepted: ["Accepted", "pill-fulfilled"], declined: ["Declined", "pill-expired"] };

  function renderRequests(requests) {
    const list = $("portal-requests");
    if (!requests.length) { list.replaceChildren(el("li", "muted", "No requests yet.")); return; }
    list.replaceChildren(...requests.map((r) => {
      const li = el("li", `portal-request request-${r.status}`);
      const [label, pill] = STATUS[r.status] || [r.status, ""];
      const head = el("div", "card-head");
      head.append(el("strong", "", KIND[r.kind] || r.kind), el("span", `status-pill ${pill}`, label));
      li.append(head, el("p", "small no-margin", r.summary));
      if (r.status !== "open") {
        li.append(el("p", "muted small no-margin",
          `${r.decided_by}${r.note ? `: ${r.note}` : ""}`));
      }
      return li;
    }));
  }

  async function refresh() {
    const data = await getJson("/api/portal");
    if (data.ok) renderRequests(data.requests);
  }

  $("portal-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const box = $("portal-message");
    const message = box.value.trim();
    if (!message) return;
    const button = event.submitter;
    button.disabled = true;
    const reply = $("portal-reply");
    reply.hidden = false;
    reply.className = "portal-reply";
    reply.textContent = "Sending…";
    try {
      const result = await postJson("/api/portal/messages", { message });
      reply.textContent = result.reply;
      reply.classList.add(result.ok ? "ok" : "deny");
      if (result.ok) box.value = "";
      await refresh();
    } catch (exc) {
      reply.textContent = `✕ ${exc.message}`;
      reply.classList.add("deny");
    } finally {
      button.disabled = false;
    }
  });

  document.querySelectorAll("#portal-form [data-say]").forEach((chip) => {
    chip.addEventListener("click", () => { $("portal-message").value = chip.dataset.say; });
  });

  $("consent").addEventListener("change", async (event) => {
    const share = event.target.checked;
    try {
      const result = await postJson("/api/portal/consent", { share_sensitive_with_pcp: share });
      $("consent-note").textContent = result.consent.note;
    } catch (exc) {
      event.target.checked = !share;
      $("consent-note").textContent = `✕ ${exc.message}`;
    }
  });

  renderRequests(window.PORTAL_REQUESTS || []);
  setInterval(refresh, 10000);   // see the doctor's decision without reloading
})();
