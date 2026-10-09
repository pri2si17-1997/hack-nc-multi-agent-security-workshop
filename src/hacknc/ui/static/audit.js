// Audit page: hash-chained log + verify button.
"use strict";

(() => {
  const { el, getJson, postJson } = window.HackNC;
  const rows = document.getElementById("audit-rows");
  const count = document.getElementById("audit-count");
  const decision = document.getElementById("filter-decision");
  const highOnly = document.getElementById("filter-high");
  const includeOpa = document.getElementById("filter-opa");
  const attacksOnly = document.getElementById("filter-attacks");
  const autoRefresh = document.getElementById("auto-refresh");
  const verifyButton = document.getElementById("verify");
  const verifyResult = document.getElementById("verify-result");

  function cell(content, className) {
    const td = el("td", className || "");
    if (content instanceof Node) td.append(content);
    else td.textContent = content ?? "";
    return td;
  }

  function row(entry) {
    const tr = el("tr", `sev-${entry.severity}${entry.attack ? " staged-attack" : ""}`);
    const time = new Date(entry.recorded_at).toLocaleTimeString();
    const who = el("span");
    who.append(el("strong", "", entry.actor_user || "—"));
    if (entry.actor_role) who.append(el("span", "muted small", ` ${entry.actor_role}`));
    const action = el("span");
    action.append(el("code", "", entry.action), el("div", "muted small", `by ${entry.writer}`));
    if (entry.attack) {
      // Workshop label (see audit_views.attack_notes): which staged attack this row belongs to.
      const note = el("span", "attack-pill", `🎭 ${entry.attack.label}`);
      note.title = `Staged attack: ${entry.attack.style}`;
      action.append(note);
    }
    const badge = el("span", `verdict verdict-${entry.decision === "deny" ? "deny" : entry.decision === "allow" ? "ok" : "info"}`,
      entry.decision.toUpperCase());
    const hash = el("code", "hash", entry.hash.slice(0, 8));
    hash.title = `prev ${entry.prev_hash.slice(0, 16)}…\nhash ${entry.hash}`;
    let trace = "";
    if (entry.trace_url) {
      trace = el("a", "trace-link", "trace ↗");
      trace.href = entry.trace_url;
      trace.target = "_blank";
      trace.rel = "noopener";
    }
    tr.append(
      cell(String(entry.seq), "num"),
      cell(time, "nowrap"),
      cell(who),
      cell((entry.agent_chain || []).join(" → "), "chain-cell"),
      cell(action),
      cell(badge),
      cell(entry.reason, "reason-cell"),
      cell(hash),
      cell(trace),
    );
    return tr;
  }

  async function load() {
    const params = new URLSearchParams();
    if (decision.value) params.set("decision", decision.value);
    if (highOnly.checked) params.set("severity", "high");
    if (includeOpa.checked) params.set("include_opa", "true");
    try {
      const data = await getJson(`/api/admin/audit/events?${params}`);
      if (!data.ok) {
        rows.replaceChildren(el("tr", "", ""));
        rows.firstChild.append(cell(`✕ ${data.reason}`, "deny-text"));
        return;
      }
      const entries = attacksOnly.checked ? data.entries.filter((e) => e.attack) : data.entries;
      count.textContent = `(${entries.length} shown of ${data.total})`;
      if (!entries.length) {
        const empty = el("tr");
        const td = cell(attacksOnly.checked
          ? "No staged attack in these entries. Stage one in the admin console, then run a discharge."
          : "Nothing yet. Log in and send a message on the ward.", "muted");
        td.colSpan = 9;
        empty.append(td);
        rows.replaceChildren(empty);
        return;
      }
      rows.replaceChildren(...entries.map(row));
    } catch (err) {
      count.textContent = err.message;
    }
  }

  verifyButton.addEventListener("click", async () => {
    verifyButton.disabled = true;
    try {
      const result = await postJson("/api/admin/audit/verify");
      verifyResult.hidden = false;
      verifyResult.replaceChildren();
      const chain = result.chain;
      if (!result.ok) {
        verifyResult.className = "verify-result broken";
        verifyResult.append(el("strong", "", "Could not verify: "), result.reason);
      } else if (chain.ok) {
        verifyResult.className = "verify-result intact";
        verifyResult.append(el("strong", "", "✓ Chain intact"),
          ` · ${chain.checked} entries checked · head ${chain.head_hash.slice(0, 16)}…`);
      } else {
        verifyResult.className = "verify-result broken";
        verifyResult.append(el("strong", "", `✕ Chain broken at entry #${chain.broken_at}`),
          ` · ${chain.problem} · ${chain.checked} entries verified before it`);
      }
    } finally {
      verifyButton.disabled = false;
    }
  });

  async function loadDisclosures() {
    const body = document.getElementById("disclosure-rows");
    const data = await getJson("/api/admin/disclosures");
    document.getElementById("disclosure-total").textContent = `(${data.disclosures.length})`;
    if (!data.disclosures.length) {
      const empty = el("tr");
      const td = cell("Nothing has left the hospital yet.", "muted");
      td.colSpan = 8;
      empty.append(td);
      body.replaceChildren(empty);
      return;
    }
    body.replaceChildren(...data.disclosures.map((d) => {
      const tr = el("tr", d.decision === "allow" ? "" : "sev-warning");
      const sent = cell(d.decision === "allow" ? d.fields.join(", ") : `⛔ ${d.reason}`);
      if (d.payload) {
        const details = el("details");
        details.append(el("summary", "small", "payload that left"),
                       el("pre", "received-body", JSON.stringify(d.payload, null, 2)));
        sent.append(details);
      }
      const chain = [...(d.chain || [d.agent]), d.partner].filter(Boolean).join(" → ");
      tr.append(cell(String(d.seq), "num"), cell(new Date(d.at).toLocaleTimeString(), "nowrap"),
        cell(d.patient_id), cell(d.destination), cell(d.purpose), sent,
        cell(d.withheld.join(", ") || "—", d.withheld.length ? "warn-text" : ""),
        cell(`${chain} · for ${d.for}`, "chain-cell"));
      return tr;
    }));
  }

  async function loadReceived() {
    const box = document.getElementById("received");
    const data = await getJson("/api/admin/received");
    if (!data.received.length) { box.replaceChildren(el("p", "muted", "No partner has received anything yet.")); return; }
    box.replaceChildren(...data.received.slice().reverse().map((r) => {
      const card = el("div", "received-card");
      card.append(el("strong", "", r.partner_name),
        el("div", "muted small", `${r.at.slice(11)} · ${r.path} · sent by ${r.sent_by} for ${r.on_behalf_of}`),
        el("div", "small", `Fields: ${r.fields.join(", ")}`));
      const pre = el("pre", "received-body", JSON.stringify(r.body, null, 2));
      const details = el("details");
      details.append(el("summary", "small", "payload"), pre);
      card.append(details);
      return card;
    }));
  }

  [decision, highOnly, includeOpa, attacksOnly].forEach((input) => input.addEventListener("change", load));
  load();
  loadDisclosures();
  loadReceived();
  setInterval(() => { if (autoRefresh.checked) { loadDisclosures(); loadReceived(); } }, 5000);
  setInterval(() => { if (autoRefresh.checked) load(); }, 3000);
})();
