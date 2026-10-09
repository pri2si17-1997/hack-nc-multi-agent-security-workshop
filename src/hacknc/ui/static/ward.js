// The ward screen: pick a patient, talk to the discharge coordinator, sign inline.
// Every message and every signature is a run that the activity panel follows (activity.js).
"use strict";

(() => {
  const { el, getJson, postJson } = window.HackNC;
  const $ = (id) => document.getElementById(id);
  const ward = document.querySelector(".ward");
  const messages = $("messages");
  const reopenButton = $("reopen");

  const STATUS = {
    waiting: ["Waiting", "pill-waiting"], running: ["Running", "pill-running"],
    done: ["Done", "pill-fulfilled"], needs_signature: ["Needs your signature", "pill-pending"],
    sent: ["Sent", "pill-fulfilled"], blocked: ["Blocked", "pill-expired"],
    disabled: ["Turned off", "pill-expired"], failed: ["Failed", "pill-invalidated"],
    suspended: ["Suspended", "pill-invalidated"],
  };
  const DISCHARGE = {
    not_started: "Not started", in_progress: "In progress",
    awaiting_signatures: "Needs your signature", completed: "Discharged",
    on_hold: "On hold: security review",
  };

  // One conversation per patient, kept while the page is open.
  const conversations = new Map(); // id -> { nodes: [], card: Element|null }
  let current = null; // { id, name, first }

  // -------------------------------------------------------------- tiny safe Markdown
  function escapeHtml(text) {
    return text.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  }
  function inline(text) {
    return escapeHtml(text)
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[\s(])_(.+?)_(?=$|[\s).,;:])/g, "$1<em>$2</em>")
      .replace(/(^|[\s(])\*([^*\s][^*]*?)\*(?=$|[\s).,;:])/g, "$1<em>$2</em>");
  }
  function markdown(text) {
    const root = el("div", "md");
    let list = null;
    for (const raw of (text || "").split("\n")) {
      const line = raw.trim();
      if (/^[-*•] /.test(line)) {
        if (!list) { list = el("ul"); root.append(list); }
        const li = el("li");
        li.innerHTML = inline(line.slice(2)); // escaped first; only <strong>/<em> added
        list.append(li);
      } else if (/^#{1,4} /.test(line)) {
        list = null;
        const p = el("p");
        p.innerHTML = `<strong>${inline(line.replace(/^#+ /, ""))}</strong>`;
        root.append(p);
      } else if (line) {
        list = null;
        const p = el("p");
        p.innerHTML = inline(line);
        root.append(p);
      }
    }
    return root;
  }

  // -------------------------------------------------------------- chat messages
  function conversation(id) {
    if (!conversations.has(id)) conversations.set(id, { nodes: [], card: null });
    return conversations.get(id);
  }

  function addMessage(kind, content, id = current.id) {
    const msg = el("div", `msg msg-${kind}`);
    if (content instanceof Node) msg.append(content); else msg.textContent = content;
    conversation(id).nodes.push(msg);
    if (current && id === current.id) {
      messages.append(msg);
      messages.scrollTop = messages.scrollHeight;
    }
    return msg;
  }

  // -------------------------------------------------------------- the discharge card
  function signatureCard(sig) {
    const card = el("div", `sig-card sig-${sig.status}`);
    const head = el("div", "sig-head");
    head.append(el("strong", "", `✍ Sign: ${sig.title}`));
    if (sig.controlled) head.append(el("span", "chip chip-warn", `Schedule ${String(sig.controlled).slice(2)}`));
    card.append(head);
    if (sig.status === "awaiting_signature") {
      const actions = el("div", "sig-actions");
      const sign = el("button", "btn btn-primary btn-sm", "Sign");
      const reject = el("button", "btn btn-ghost btn-sm", "Reject");
      sign.type = reject.type = "button";
      sign.addEventListener("click", () => decide(sig, "approve", [sign, reject]));
      reject.addEventListener("click", () => decide(sig, "reject", [sign, reject]));
      actions.append(sign, reject, el("span", "muted small", "Uses your own token; no agent can sign."));
      card.append(actions);
    } else {
      const label = { signed: "Signed", sent: "Signed and sent", refused: "Signed, but sending was refused",
                      rejected: "Rejected", replaced: "Replaced by a new draft",
                      manual: "Done by hand" }[sig.status] || sig.status;
      card.append(el("p", "muted small no-margin", label + (sig.result ? ` · ${sig.result}` : "")));
    }
    return card;
  }

  // The gateway's reasons name patients and agents by id; say who they are.
  function readable(text) {
    const patients = Object.fromEntries([...document.querySelectorAll(".ward-patient")]
      .map((b) => [b.dataset.patient, b.dataset.name]));
    return text.replace(/\b(?:pt-[0-9a-f]+|jane-doe)\b/g, (id) => patients[id] || id)
               .replace(/\b[a-z]+-agent\b/g, (id) => window.HackNC.activity.agentLabel(id));
  }

  function alertCard(alert) {
    const card = el("div", `alert-card alert-${alert.status}`);
    const name = window.HackNC.activity.agentLabel(alert.agent);
    card.append(el("strong", "", {
      open: `🚨 Security alert: ${name} suspended`,
      acknowledged: `✔ Reviewed: ${name}`,
      handled_manually: `✔ Handled by hand: ${name} stays suspended`,
    }[alert.status] || alert.status));
    const stoppedBy = alert.stopped_by || "the EHR gateway";
    const caught = alert.attempted === "changing signed content"
      ? `${stoppedBy[0].toUpperCase()}${stoppedBy.slice(1)} caught it changing what you signed`
      : `${stoppedBy[0].toUpperCase()}${stoppedBy.slice(1)} blocked it from ${alert.attempted || "a tool"}${alert.patient_id ? ` for ${readable(alert.patient_id)}` : ""}`;
    card.append(el("p", "small no-margin",
      `${caught}: ${readable(alert.reason)}. Its result is not used; the discharge is on hold.`));
    if (alert.status === "open") {
      const actions = el("div", "sig-actions");
      const ack = el("button", "btn btn-danger btn-sm", "Acknowledge & retry");
      const manual = el("button", "btn btn-ghost btn-sm", "Handle manually");
      ack.type = manual.type = "button";
      ack.addEventListener("click", () => review(alert, "acknowledge", [ack, manual]));
      manual.addEventListener("click", () => review(alert, "manual", [ack, manual]));
      actions.append(ack, manual, el("span", "muted small",
        "Physicians only, audited. Retry once IT has fixed the agent (if it misbehaves again, " +
        "it is caught again); until then, do its task by hand."));
      card.append(actions);
    } else if (alert.status === "handled_manually") {
      card.append(el("p", "muted small no-margin", `${alert.by} did it by hand: ${alert.note}.`));
    } else {
      card.append(el("p", "muted small no-margin", `Reviewed by ${alert.by}; the task was retried.`));
    }
    return card;
  }

  async function review(alert, how, buttons) {
    let body;
    if (how === "manual") {
      const note = window.prompt("How will you do it outside the agents?",
        alert.task_id === "send_prescriptions" ? "Sent from the e-prescribing system"
                                                : "Done by phone");
      if (note === null) return;
      body = { note };
    }
    buttons.forEach((b) => { b.disabled = true; });
    const id = current.id;
    const started = await postJson(`/api/discharges/${id}/alerts/${alert.id}/${how}`, body);
    const result = await follow(started, how === "manual" ? "Take over by hand"
                                                           : "Review security alert");
    if (!result.ok) {
      addMessage("system", `✕ ${result.reason}`, id).classList.add("msg-refused");
      buttons.forEach((b) => { b.disabled = false; });
      return;
    }
    showDischarge(result.checklist, id);
  }

  function taskRow(task) {
    const row = el("details", `task task-${task.status}`);
    const [label, pill] = STATUS[task.status] || [task.status, ""];
    const head = el("summary", "task-head");
    head.append(el("span", "task-title", `▸ ${task.title}`),
                el("span", `status-pill ${pill}`, label));
    row.append(head);
    if (task.summary) row.append(el("p", "task-summary", task.summary));
    if (task.detail) row.append(markdown(task.detail));
    return row;
  }

  function dischargeCard(state) {
    const card = el("div", "discharge-card");
    const head = el("div", "card-head");
    head.append(el("strong", "", "Discharge"),
                el("span", `status-pill pill-${state.status}`, DISCHARGE[state.status] || state.status));
    card.append(head);
    card.append(...(state.alerts || []).map(alertCard));
    card.append(...state.tasks.filter((t) => t.status !== "waiting" || t.summary).map(taskRow));
    for (const task of state.tasks) {
      for (const sig of task.signatures) if (sig.status !== "withdrawn") card.append(signatureCard(sig));
    }
    const r = state.ehr_record;
    if (r) {
      const at = new Date(r.recorded_at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      card.append(el("p", `ehr-record ${r.recorded ? "ok-text" : "deny-text"}`, r.recorded
        ? `🏠 ${current ? current.first : "The patient"} is discharged ${r.disposition || "home"} · recorded in the EHR at ${at}`
        : `✕ Not recorded in the EHR: ${r.error}`));
    }
    return card;
  }

  function showDischarge(state, id = current.id) {
    if (!state || !state.tasks) return;
    const conv = conversation(id);
    if (id === current.id) {
      $("discharge-status").textContent = DISCHARGE[state.status] || state.status;
      $("discharge-status").className = `status-pill pill-${state.status}`;
      reopenButton.hidden = !["in_progress", "awaiting_signatures", "on_hold"].includes(state.status);
    }
    if (state.status === "not_started") return;
    const card = dischargeCard(state);
    if (conv.card) {
      conv.card.replaceChildren(card);   // one live card per patient, updated in place
    } else {
      conv.card = addMessage("card", card, id);
    }
    markWard(id, state.status);
  }

  function markWard(id, status) {
    const button = document.querySelector(`.ward-patient[data-patient="${id}"]`);
    if (!button) return;
    button.querySelector(".ward-dot").className = `ward-dot dot-${status}`;
    button.querySelector("[data-status]").textContent = DISCHARGE[status] || status;
  }

  // -------------------------------------------------------------- actions (each is a run)
  async function follow(started, label) {
    return window.HackNC.activity.follow(started, label);
  }

  async function send(text) {
    const id = current.id;
    addMessage("user", text, id);
    const bubble = addMessage("assistant", "…", id);
    bubble.classList.add("thinking");
    try {
      const started = await postJson("/api/chat", { patient_id: id, message: text });
      const result = await follow(started, `“${text}”`);
      bubble.classList.remove("thinking");
      if (!result.ok) { bubble.replaceChildren(el("span", "deny-text", `✕ ${result.reason}`)); return; }
      const body = markdown(result.reply);
      if (result.by) body.append(el("p", "byline", result.by));
      bubble.replaceChildren(body);
      if (/^🛑|Refused|may not/.test(result.reply)) bubble.classList.add("msg-refused");
      showDischarge(result.checklist, id);
    } catch (exc) {
      bubble.classList.remove("thinking");
      bubble.replaceChildren(el("span", "deny-text", `✕ ${exc.message}`));
    }
  }

  async function decide(sig, decision, buttons) {
    buttons.forEach((b) => { b.disabled = true; });
    const id = current.id;
    const verb = decision === "approve" ? "Sign" : "Reject";
    const started = await postJson(`/api/signatures/${sig.signature_id}`, { patient_id: id, decision });
    const result = await follow(started, `${verb}: ${sig.title}`);
    if (!result.ok) {
      addMessage("system", `✕ ${verb} refused: ${result.reason}`, id).classList.add("msg-refused");
      buttons.forEach((b) => { b.disabled = false; });
      return;
    }
    showDischarge(result.checklist, id);
  }

  reopenButton.addEventListener("click", async () => {
    if (!window.confirm("Reopen this discharge? Drafts that are not signed yet are cancelled, and the agents start over.")) return;
    const id = current.id;
    reopenButton.disabled = true;
    try {
      const started = await postJson(`/api/discharges/${id}/reopen`, { reason: "reopened from the ward" });
      const result = await follow(started, "Reopen discharge");
      if (!result.ok) { addMessage("system", `✕ ${result.reason}`, id).classList.add("msg-refused"); return; }
      conversation(id).card = null;
      addMessage("system", "Discharge reopened: unsigned drafts were cancelled.", id);
      showDischarge(result.checklist, id);
    } finally { reopenButton.disabled = false; }
  });

  $("chat-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const input = $("chat-input");
    const text = input.value.trim();
    if (!text || !current) return;
    input.value = "";
    send(text);
  });

  $("suggestions").addEventListener("click", (event) => {
    const chip = event.target.closest("[data-say]");
    if (!chip || !current) return;
    send(chip.dataset.say.replace("{first}", current.first).replace("{name}", current.name));
  });

  // -------------------------------------------------------------- patient portal requests
  const KIND = { medication_change: "asks to change a medicine", refill: "asks for a refill",
                 side_effect: "reports a side effect", question: "asks a question" };

  function requestCard(r, id) {
    const card = el("div", `request-card request-${r.status}`);
    const name = current ? current.first : "The patient";
    card.append(el("strong", "", `📨 ${name} ${KIND[r.kind] || r.kind} (patient portal)`));
    card.append(el("p", "small no-margin", `“${r.summary}”`));
    if (r.drug) {
      card.append(el("p", "small muted no-margin",
        `Medicine: ${r.drug}${r.requested_dose ? ` · asks for ${r.requested_dose}` : ""}`));
    }
    if ((r.flags || []).includes("prompt_injection")) {
      card.append(el("p", "small deny-text no-margin",
        "⚠ The message contains instructions aimed at AI assistants: treated as data, not followed."));
    }
    if (r.status === "open") {
      const actions = el("div", "sig-actions");
      const medicine = ["medication_change", "refill"].includes(r.kind) && r.drug;
      const accept = el("button", "btn btn-primary btn-sm", medicine ? "Accept into plan" : "Acknowledge");
      const decline = el("button", "btn btn-ghost btn-sm", "Decline");
      accept.type = decline.type = "button";
      accept.addEventListener("click", () => decideRequest(r, true, [accept, decline], id));
      decline.addEventListener("click", () => decideRequest(r, false, [accept, decline], id));
      actions.append(accept);
      if (medicine) actions.append(decline);
      actions.append(el("span", "muted small", medicine
        ? "Accepting adds it to the discharge plan; reconciliation and your signature still apply."
        : "Physicians only. Your decision is audited."));
      card.append(actions);
    } else {
      card.append(el("p", "muted small no-margin",
        `${r.status === "accepted" ? "Accepted" : "Declined"} by ${r.decided_by}${r.note ? `: ${r.note}` : ""}`));
    }
    return card;
  }

  async function loadRequests(id) {
    const data = await getJson(`/api/requests/${id}`);
    if (!data.ok) return;
    const conv = conversation(id);
    conv.requests = conv.requests || new Map();
    for (const r of data.requests) {
      const card = requestCard(r, id);
      if (conv.requests.has(r.id)) conv.requests.get(r.id).replaceChildren(card);
      else conv.requests.set(r.id, addMessage("card", card, id));
    }
  }

  async function decideRequest(r, accept, buttons, id) {
    let note = "";
    if (!accept) {
      note = window.prompt("Reason for the patient (they will see it):", "") ?? null;
      if (note === null) return;
    }
    buttons.forEach((b) => { b.disabled = true; });
    const started = await postJson(`/api/requests/${id}/${r.id}`, { accept, note });
    const result = await follow(started, `${accept ? "Accept" : "Decline"} patient request`);
    if (!result.ok) {
      addMessage("system", `✕ ${result.reason}`, id).classList.add("msg-refused");
      buttons.forEach((b) => { b.disabled = false; });
      return;
    }
    if (result.hint) addMessage("system", result.hint, id);
    loadRequests(id);
  }

  // -------------------------------------------------------------- selecting a patient
  async function select(button) {
    document.querySelectorAll(".ward-patient").forEach((b) => b.classList.toggle("selected", b === button));
    const name = button.dataset.name;
    current = { id: button.dataset.patient, name, first: name.split(" ")[0] };
    $("patient-name").textContent = name;
    $("patient-facts").textContent = `${button.dataset.age} y · ${button.dataset.gender} · ${button.dataset.bed} · ${button.dataset.reason}`;
    document.querySelectorAll("#suggestions [data-say]").forEach((chip) => {
      chip.textContent = chip.textContent.replace(/Discharge .*/, `Discharge ${current.first}`);
    });
    const conv = conversation(current.id);
    if (!conv.nodes.length) {
      addMessage("system", `Talking about ${name}. Ask a question, or say “Discharge ${name}”.`);
    }
    messages.replaceChildren(...conv.nodes);
    messages.scrollTop = messages.scrollHeight;
    history.replaceState(null, "", `/ward?patient=${current.id}`);
    showDischarge(await getJson(`/api/discharges/${current.id}`));
    loadRequests(current.id);
  }

  document.querySelectorAll(".ward-patient").forEach((button) => {
    button.addEventListener("click", () => select(button));
  });
  const first = document.querySelector(`.ward-patient[data-patient="${ward.dataset.selected}"]`)
    || document.querySelector(".ward-patient");
  if (first) select(first);

  // -------------------------------------------------------------- chart drawer
  const drawer = $("chart-drawer"), backdrop = $("drawer-backdrop");

  function closeDrawer() { drawer.hidden = true; backdrop.hidden = true; }

  async function loadAccessLog(list) {
    const data = await getJson(`/api/access-log/${list.dataset.patient}`);
    if (!data.ok) { list.replaceChildren(el("li", "deny-text", data.reason)); return; }
    if (!data.entries.length) { list.replaceChildren(el("li", "muted", "No access recorded yet.")); return; }
    list.replaceChildren(...data.entries.map((e) => {
      const li = el("li", `access access-${e.decision} sev-${e.severity}`);
      const who = e.who ? `${e.who}${e.role ? ` (${e.role})` : ""}` : (e.writer || "system");
      const via = (e.via || []).map((a) => window.HackNC.activity.agentLabel(a)).join(" → ");
      const head = el("div", "access-head");
      head.append(el("span", "access-icon", e.decision === "deny" ? "⛔" : e.severity === "high" ? "⚠" : "•"),
                  el("strong", "", who), el("span", "", ` ${e.what}`),
                  el("span", "muted small", ` · ${e.at.slice(11, 19)} · #${e.seq}`));
      li.append(head);
      if (via) li.append(el("div", "small muted", `via ${via}`));
      li.append(el("div", "small", e.reason));
      return li;
    }));
  }

  async function breakGlass(patientId) {
    const justification = window.prompt(
      "Emergency access. Why do you need the withheld records? (at least 15 characters; reviewed later)", "");
    if (justification === null) return;
    const result = await postJson(`/api/break-glass/${patientId}`, { justification });
    if (!result.ok) { window.alert(`Refused: ${result.reason}`); return; }
    openChart();   // reload: the gateway now sees the grant (and flags every access)
  }

  async function openChart() {
    if (!current) return;
    $("drawer-body").replaceChildren(el("p", "muted", "Loading the chart…"));
    drawer.hidden = false; backdrop.hidden = false;
    const response = await fetch(`/ward/${current.id}/chart`);
    // The fragment is rendered by our own server with Jinja autoescaping on.
    $("drawer-body").innerHTML = await response.text();
    drawer.querySelector("[data-close-drawer]")?.addEventListener("click", closeDrawer);
    drawer.querySelector("[data-break-glass]")?.addEventListener("click",
      (event) => breakGlass(event.target.dataset.breakGlass));
    drawer.querySelectorAll(".drawer-tabs [data-tab]").forEach((tab) => {
      tab.addEventListener("click", () => {
        drawer.querySelectorAll(".drawer-tabs [data-tab]").forEach((t) => t.classList.toggle("active", t === tab));
        drawer.querySelectorAll(".drawer-panel").forEach((p) => { p.hidden = p.dataset.panel !== tab.dataset.tab; });
        if (tab.dataset.tab === "access") loadAccessLog($("access-log"));
      });
    });
  }

  $("open-chart").addEventListener("click", openChart);
  backdrop.addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeDrawer(); });
})();
