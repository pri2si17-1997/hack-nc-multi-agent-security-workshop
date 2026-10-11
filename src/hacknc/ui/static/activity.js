// Activity: follows one run (a trace) live, as a map and as a feed grouped by agent.
//
//   const result = await HackNC.activity.follow(started, "Discharge Jane Doe");
//
// Steps stream in over Server-Sent Events, sorted by start time. Each step may name a map
// edge (from -> to): it animates, and turns red with a ✕ when something was refused.
// In Step mode the presenter reveals one step per click; the run's result is held back
// until after the last step.
"use strict";

(() => {
  const { el } = window.HackNC;
  const $ = (id) => document.getElementById(id);
  const SVG = "http://www.w3.org/2000/svg";
  const svg = $("live-map");
  const feed = $("feed");
  const stepMode = $("step-mode");
  const nextButton = $("next-step");
  const nextAgent = $("next-agent");
  const status = $("steps-status");
  const caption = $("map-caption");

  // ------------------------------------------------------------ the map's fixed nodes
  const W = 92, H = 30;
  const AGENT_ICON = "🤖";
  const NODES = {
    user: [8, 20, "🧑 Clinician", "person"],
    orchestrator: [8, 110, "Coordinator", "agent"],
    model: [8, 210, "✨ LLM", "model"],
    keycloak: [8, 310, "🔑 Keycloak", "service"],
    audit: [8, 410, "📝 Audit", "service"],
    "medrec-agent": [132, 20, "Med rec", "agent"],
    "benefits-agent": [132, 74, "Benefits", "agent"],
    "scheduling-agent": [132, 128, "Scheduling", "agent"],
    "prescription-agent": [132, 182, "Prescriptions", "agent"],
    "summary-agent": [132, 236, "Summary", "agent"],
    "instructions-agent": [132, 290, "Instructions", "agent"],
    "transitions-agent": [132, 344, "Transitions", "agent"],
    "chart-agent": [132, 398, "Chart Q&A", "agent"],
    "ehr-mcp": [256, 90, "🗄 EHR gw", "gateway"],
    opa: [256, 190, "🛡 OPA", "service"],
    "disclosure-mcp": [256, 290, "📤 Disclosure gw", "gateway"],
    approvals: [256, 410, "✍ Signatures", "service"],
    payer: [362, 200, "Payer", "partner"],
    "retail-pharmacy": [362, 254, "Pharmacy", "partner"],
    "pcp-clinic": [362, 308, "Clinic", "partner"],
    "nc-csrs": [362, 362, "NC registry", "partner"],
  };
  const AGENT_NAMES = {
    user: "You", orchestrator: "Discharge coordinator", "medrec-agent": "Med-rec agent",
    "benefits-agent": "Benefits agent", "scheduling-agent": "Scheduling agent",
    "prescription-agent": "Prescription agent", "summary-agent": "Summary agent",
    "instructions-agent": "Instructions agent", "transitions-agent": "Care-transitions agent",
    "chart-agent": "Chart agent", "portal-agent": "Portal agent", "hospital-ui": "Hospital app", "ehr-mcp": "EHR gateway",
    "disclosure-mcp": "Disclosure gateway", approvals: "Signature service", keycloak: "Keycloak",
    audit: "Audit trail", opa: "OPA",
  };
  // What each node is supposed to do: the map's hover tooltip.
  const ROLES = {
    user: "You. Every agent acts for a signed-in person and can do no more than they may.",
    orchestrator: "Plans the discharge and hands each task to one agent, binding that agent to this patient at the EHR gateway. Suspends an agent the gateway catches overstepping.",
    model: "The language model the agents reason with. Rules check its work, and take over when it is off or wrong.",
    keycloak: "Signs people in and exchanges their token for a narrower one per agent: only the scopes that task needs.",
    audit: "Hash-chained log of every decision, written by the gateways. Editing an entry breaks the chain.",
    "medrec-agent": "Compares home, hospital and planned discharge medications and flags discrepancies for the physician.",
    "benefits-agent": "Checks insurance coverage and requests prior authorization for home equipment (e.g. oxygen). Only for this patient.",
    "scheduling-agent": "Books the follow-up visit the discharge plan asks for.",
    "prescription-agent": "Drafts e-prescriptions from the verified medication plan, checks controlled substances with the state registry, and sends them to the pharmacy once signed.",
    "summary-agent": "Drafts the discharge summary and puts it in the physician's signature queue.",
    "instructions-agent": "Writes plain-language discharge instructions and posts them to the patient portal.",
    "transitions-agent": "Sends the signed discharge summary to the patient's family doctor.",
    "chart-agent": "Answers a clinician's questions from the chart, showing only what that clinician may see.",
    "ehr-mcp": "The only way into patient records. Checks each call's token and task with OPA, and audits it.",
    opa: "The policy engine. Decides allow or deny for each gateway call: role, scopes, patient assignment, consent.",
    "disclosure-mcp": "The only way out to other organizations. Checks consent and withholds anything the partner doesn't need.",
    approvals: "The physician's signature queue. No agent can sign; only a physician's own token can.",
    payer: "Blue Cross Blue Shield: decides prior authorization requests.",
    "retail-pharmacy": "Main Street Pharmacy: receives signed e-prescriptions.",
    "pcp-clinic": "Maple Family Medicine (Dr. Lee): receives the signed discharge summary.",
    "nc-csrs": "NC controlled-substance registry: prescription history for controlled drugs.",
  };
  const isAgent = (id) => NODES[id]?.[3] === "agent" || /-agent$/.test(id || "");
  const agentLabel = (id) => (isAgent(id) ? `${AGENT_ICON} ` : "") + (AGENT_NAMES[id] || id);
  const center = (id) => { const [x, y] = NODES[id]; return [x + W / 2, y + H / 2]; };

  function svgEl(tag, attrs, text) {
    const node = document.createElementNS(SVG, tag);
    for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, v);
    if (text !== undefined) node.textContent = text;
    return node;
  }

  const edgeLayer = svgEl("g", { class: "edges" });
  const nodeLayer = svgEl("g", { class: "nodes" });
  svg.append(edgeLayer, nodeLayer);
  const measure = document.createElement("canvas").getContext("2d");
  for (const [id, [x, y, name, kind]] of Object.entries(NODES)) {
    const label = kind === "agent" ? `${AGENT_ICON} ${name}` : name;
    const g = svgEl("g", { class: `map-node kind-${kind}`, "data-node": id });
    const text = svgEl("text", { x: x + W / 2, y: y + H / 2 + 4, "text-anchor": "middle" }, label);
    g.append(svgEl("rect", { x, y, width: W, height: H, rx: 8 }), text);
    nodeLayer.append(g);
    // Squeeze a long label into its box (measured on a canvas: the map may still be hidden).
    const style = getComputedStyle(text);
    measure.font = `${style.fontWeight} ${style.fontSize} ${style.fontFamily}`;
    if (measure.measureText(label).width > W - 10) {
      text.setAttribute("textLength", W - 10);
      text.setAttribute("lengthAdjust", "spacingAndGlyphs");
    }
  }

  // Hover a node: who it is and what it is supposed to do.
  const tip = el("div", "map-tip");
  tip.hidden = true;
  svg.after(tip);
  nodeLayer.addEventListener("mouseover", (event) => {
    const node = event.target.closest(".map-node");
    if (!node) return;
    const id = node.dataset.node;
    tip.replaceChildren(el("strong", "", isAgent(id) ? agentLabel(id) : NODES[id][2]),
                        el("p", "", ROLES[id] || ""),
                        ...(id === "model" ? [el("p", "", `Model: ${svg.dataset.llm || "unknown"}`)] : []));
    tip.hidden = false;
  });
  nodeLayer.addEventListener("mousemove", (event) => {
    const box = svg.parentElement.getBoundingClientRect();
    const left = Math.min(event.clientX - box.left + 14, box.width - tip.offsetWidth - 4);
    tip.style.left = `${Math.max(4, left)}px`;
    tip.style.top = `${event.clientY - box.top + 14}px`;
  });
  nodeLayer.addEventListener("mouseout", (event) => {
    if (!event.relatedTarget?.closest?.(".map-node")) tip.hidden = true;
  });

  function drawEdges(visible) {
    edgeLayer.replaceChildren();
    const seen = new Map(); // "a>b" -> worst status so far
    visible.forEach((step) => {
      if (!step.from || !step.to || !NODES[step.from] || !NODES[step.to]) return;
      const key = `${step.from}>${step.to}`;
      const rank = { info: 0, ok: 0, warn: 1, deny: 2 };
      if (!seen.has(key) || rank[step.status] >= rank[seen.get(key).status]) seen.set(key, step);
    });
    const latest = visible[visible.length - 1];
    for (const [key, step] of seen) {
      const [x1, y1] = center(step.from), [x2, y2] = center(step.to);
      const live = latest && `${latest.from}>${latest.to}` === key;
      edgeLayer.append(svgEl("line", { x1, y1, x2, y2,
        class: `edge edge-${step.status}${live ? " edge-live" : ""}` }));
      if (step.status === "deny") {
        edgeLayer.append(svgEl("text", { x: (x1 + x2) / 2, y: (y1 + y2) / 2 + 5,
          class: "edge-x", "text-anchor": "middle" }, "✕"));
      }
    }
  }

  function lightNodes(visible) {
    nodeLayer.querySelectorAll(".map-node").forEach((n) => n.removeAttribute("data-state"));
    const mark = (id, state) => {
      const node = nodeLayer.querySelector(`[data-node="${id}"]`);
      if (!node) return;
      if (node.dataset.state === "deny" && state !== "deny") return;
      node.dataset.state = state;
    };
    visible.forEach((step, i) => {
      for (const other of step.also || []) mark(other, "visited");
      if (step.from) mark(step.from, "visited");
      const last = i === visible.length - 1;
      mark(step.node, step.status === "deny" ? "deny" : last ? "active" : "visited");
    });
  }

  // ------------------------------------------------------------ the feed, grouped by actor
  function feedLine(step, n) {
    const li = el("li", `feed-line status-${step.status} pillar-${step.pillar}`);
    const head = el("div", "feed-head");
    head.append(el("span", "step-num", String(n)), el("span", "step-icon", step.icon),
                el("span", "feed-title", step.title));
    li.append(head);
    if (step.caption) li.append(el("p", "feed-caption", step.caption));
    const keys = Object.keys(step.details || {});
    if (keys.length || step.trace_url) {
      const details = el("details", "step-details");
      details.append(el("summary", "", "details"));
      const dl = el("dl");
      for (const key of keys) dl.append(el("dt", "", key), el("dd", "", step.details[key]));
      details.append(dl);
      // The raw span this step was built from, selected in Grafana's trace view.
      if (step.trace_url) {
        const link = el("a", "trace-link", "this span in Grafana ↗");
        link.href = step.trace_url;
        link.target = "_blank";
        link.rel = "noopener";
        details.append(link);
      }
      li.append(details);
    }
    return li;
  }

  function renderFeed(visible) {
    const groups = new Map();
    visible.forEach((step, i) => {
      const actor = step.actor || step.node;
      if (!groups.has(actor)) groups.set(actor, []);
      groups.get(actor).push([step, i + 1]);
    });
    feed.replaceChildren(...[...groups].map(([actor, steps]) => {
      const section = el("section", "feed-group");
      const blocked = steps.filter(([s]) => s.status === "deny").length;
      const head = el("h3", "feed-actor", agentLabel(actor));
      head.append(el("span", "muted small", ` · ${steps.length} step(s)`));
      if (blocked) head.append(el("span", "chip chip-deny", `${blocked} blocked`));
      const list = el("ol", "feed-lines");
      list.append(...steps.map(([s, n]) => feedLine(s, n)));
      section.append(head, list);
      return section;
    }));
  }

  // ------------------------------------------------------------ following a run
  let run = null; // { steps, revealed, result, released, resolve, done }

  function release() {
    if (!run || !run.result || run.released) return;
    run.released = true;
    run.resolve(run.result);
  }

  function render() {
    if (!run) return;
    const total = run.steps.length;
    const shown = stepMode.checked ? Math.min(run.revealed, total) : total;
    const visible = run.steps.slice(0, shown);
    drawEdges(visible);
    lightNodes(visible);
    renderFeed(visible);
    const latest = visible[visible.length - 1];
    caption.textContent = latest ? `${latest.icon} ${latest.title}` : "";
    caption.className = `map-caption status-${latest ? latest.status : "info"}`;

    const pending = total - shown;
    nextButton.hidden = !stepMode.checked;
    nextAgent.hidden = !stepMode.checked || pending === 0;
    nextButton.disabled = pending === 0 && (run.released || !run.result);
    nextButton.textContent = pending > 0 ? `Next ▸ (${pending} waiting)`
      : (run.result && !run.released ? "Show result ▸" : "Next ▸");
    status.textContent = total ? `${shown} of ${total} steps${run.done ? "" : " · live"}`
      : "Waiting for the first step…";
    if (!stepMode.checked) release();
  }

  function follow(started, label) {
    $("activity-label").textContent = label;
    status.textContent = "Waiting for the first step…";
    return new Promise((resolve) => {
      const thisRun = { steps: [], revealed: 0, result: null, released: false, resolve,
                        done: false };
      run = thisRun;
      render();
      const source = new EventSource(`/api/runs/${started.run_id}/events`);
      source.onmessage = (message) => {
        if (run !== thisRun) { source.close(); return; }
        const event = JSON.parse(message.data);
        if (event.type === "step") {
          if (!run.steps.some((s) => s.id === event.id)) {
            run.steps.push(event);
            run.steps.sort((a, b) => a.t - b.t);
          }
        } else if (event.type === "answer") {
          run.result = event;
        } else if (event.type === "done" || event.type === "error") {
          run.done = true;
          source.close();
        }
        render();
      };
      source.onerror = () => source.close();
    });
  }

  nextButton.addEventListener("click", () => {
    if (!run) return;
    if (run.revealed < run.steps.length) run.revealed += 1;
    else release();
    render();
  });

  // Reveal everything up to (and including) the next agent's first step.
  nextAgent.addEventListener("click", () => {
    if (!run) return;
    let i = run.revealed;
    while (i < run.steps.length) {
      i += 1;
      const next = run.steps[i];
      if (next && next.icon === "🤖") break;
    }
    run.revealed = Math.min(i, run.steps.length);
    render();
  });

  stepMode.addEventListener("change", () => {
    if (run && !stepMode.checked) run.revealed = run.steps.length;
    render();
    try { localStorage.setItem("hacknc-step-mode", stepMode.checked ? "1" : "0"); } catch { /* ignore */ }
  });
  try { stepMode.checked = localStorage.getItem("hacknc-step-mode") === "1"; } catch { /* ignore */ }
  nextButton.hidden = !stepMode.checked;

  // Map | Feed tabs
  document.querySelectorAll(".activity-head [data-view]").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".activity-head [data-view]").forEach((t) =>
        t.classList.toggle("active", t === tab));
      document.querySelectorAll(".activity-view").forEach((v) => {
        v.hidden = v.dataset.view !== tab.dataset.view;
      });
    });
  });

  window.HackNC.activity = { follow, agentName: (id) => AGENT_NAMES[id] || id, agentLabel };
})();
