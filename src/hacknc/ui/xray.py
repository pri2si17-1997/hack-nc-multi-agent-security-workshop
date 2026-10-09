"""Turn trace spans into the human-readable steps of the activity map and feed.

Each step belongs to one of the workshop's pillars, names the ``actor`` it is about (the feed
groups by actor), and may name an edge of the live map (``from`` → ``to``) that animates,
red when something was refused:

    privilege       🔑 identity and tokens (Keycloak)
    trust           🛡 policy decisions and data access (OPA, ehr-mcp, approvals)
    accountability  📝 audit trail
    agent           🧭 what the agents themselves do (plan, work, ask the model, answer)

Only spans our services annotate become steps; everything else (HTTP plumbing, OPA
internals) stays in Grafana for whoever wants the full detail.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from hacknc.ui.otlp import SpanRecord

AGENT_NAMES = {"orchestrator": "Discharge coordinator", "medrec-agent": "Med-rec agent",
               "summary-agent": "Summary agent", "prescription-agent": "Prescription agent",
               "benefits-agent": "Benefits agent", "scheduling-agent": "Scheduling agent",
               "transitions-agent": "Care-transitions agent",
               "instructions-agent": "Instructions agent", "chart-agent": "Chart agent",
               "portal-agent": "Portal agent", "hospital-ui": "Hospital app",
               "ehr-mcp": "EHR gateway",
               "disclosure-mcp": "Disclosure gateway"}
PARTNER_NAMES = {"payer": "Blue Cross Blue Shield", "retail-pharmacy": "Main Street Pharmacy",
                 "pcp-clinic": "Maple Family Medicine", "nc-csrs": "NC registry"}
TOOL_PURPOSE = {
    "search_patients": "look up demographics", "get_patient_summary": "read problems and vitals",
    "get_medications": "read medications", "get_allergies": "read allergies",
    "get_observations": "read labs and vitals", "get_clinical_notes": "read clinical notes",
    "get_portal_messages": "read the patient's portal messages",
    "get_coverage": "read insurance coverage",
    "get_discharge_plan": "read the discharge plan only",
    "request_signature": "queue a draft for signature",
    "post_patient_instructions": "post instructions to the portal",
    "record_discharge": "record the discharge in the EHR",
}
# Audit entries worth a step of their own (every tool call is audited too, but showing each
# would bury the story; they are all in the Audit page and Grafana). The Collector tags the
# same spans story=true for Grafana (transform/story in otel-collector.yaml): keep in sync.
STORY_AUDIT_ACTIONS = {"discharge.started", "disclosure.sent", "disclosure.blocked",
                       "signature.requested", "approval.approve", "approval.reject",
                       "approval.tamper_detected", "break_glass.granted", "break_glass.access",
                       "encounter.discharged", "discharge.reopened", "discharge.refused",
                       "security.alert_acknowledged", "patient_request.decided",
                       "patient_request.submitted", "consent.changed"}
ACTION_CAPTIONS = {
    "sign": "The hospital app exchanges {user}'s token for one addressed to the signature "
            "service. Only physicians get rx:approve from Keycloak.",
    "decide_request": "Only a physician (discharge:order) decides a patient's request; the "
                      "decision is written to the EHR by the coordinator and audited.",
    "acknowledge": "Only a physician (discharge:order) may clear a security alert; the "
                   "review is audited with {user}'s name, then the task is retried.",
    "chat": "{user}'s message goes to the discharge coordinator with {user}'s login token, "
            "addressed to the coordinator only. The coordinator decides what to do.",
    "reopen": "Only a physician (discharge:order) may reopen; drafts nobody signed are "
              "cancelled with {user}'s own authority.",
}


@dataclass(frozen=True)
class Step:
    id: str
    t: int
    icon: str
    title: str
    caption: str
    pillar: str
    status: str            # ok | warn | deny | info
    node: str              # map node to light up
    details: dict[str, str] = field(default_factory=dict)
    also: tuple[str, ...] = ()   # other nodes involved (e.g. OPA behind the gateway)
    actor: str = ""              # who this step is about (feed groups by it)
    span: str = ""               # the span it came from (for "open in Grafana")
    src: str = ""                # map edge: from ...
    dst: str = ""                # ... to

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["from"], data["to"] = data.pop("src"), data.pop("dst")
        data["actor"] = data["actor"] or data["node"]
        return data


def name(agent: str) -> str:
    return AGENT_NAMES.get(agent, agent)


def steps_for(span: SpanRecord) -> list[Step]:
    """Zero, one or two steps for a span."""
    for matches, build in _RULES:
        if matches(span):
            return build(span)
    return []


# --------------------------------------------------------------------------- builders


def _step(span: SpanRecord, suffix: str = "", **fields: Any) -> Step:
    details = {k: str(v) for k, v in fields.pop("details", {}).items() if v not in ("", None)}
    return Step(id=span.span_id + suffix, t=span.start_ns + (1 if suffix else 0),
                span=span.span_id, details=details, **fields)


def _ask(span: SpanRecord) -> list[Step]:
    user = span.attr("user.name")
    action = span.attr("ui.action") or "chat"
    title = {"reopen": f"{user} reopens the discharge",
             "sign": f"{user}: {span.attr('question')}",
             "chat": f"{user}: “{span.attr('question')}”",
             "acknowledge": f"{user} reviews the security alert",
             "decide_request": f"{user}: {span.attr('question')}"}.get(
        action, f"{user}: “{span.attr('question')}”")
    return [_step(span, icon="🧑", node="user", pillar="privilege", status="info", title=title,
                  actor="user", src="user", dst="orchestrator",
                  caption=ACTION_CAPTIONS.get(action, ACTION_CAPTIONS["chat"]).format(user=user),
                  details={"user": user, "role": span.attr("user.role"),
                           "token scopes": span.attr("token.scope")})]


def _plan(span: SpanRecord) -> list[Step]:
    agents = [a for a in str(span.attr("plan.agents") or "").split(",") if a]
    caption = (f"{len(agents)} agents: {', '.join(name(a) for a in agents)}. " if agents
               else "")
    return [_step(span, icon="🧭", node="orchestrator", pillar="agent", status="info",
                  actor="orchestrator",
                  title=f"Coordinator plans: {span.attr('plan.reason')}",
                  caption=caption + "The coordinator holds no patient data and no data "
                                    "permissions.",
                  details={"reasoning": span.attr("agent.reasoning"),
                           "why": span.attr("plan.reason")})]


def _exchange(span: SpanRecord) -> list[Step]:
    requester = span.attr("token.requester")
    audience = span.attr("token.requested_audience")
    user = span.attr("user.name")
    details = {"requested by (azp)": span.attr("token.azp") or requester,
               "for (aud)": span.attr("token.aud") or audience,
               "acting for (sub)": user, "scopes granted": span.attr("token.scope"),
               "scopes requested": span.attr("token.requested_scopes")}
    if span.attr("token.granted") is True:
        return [_step(span, icon="🔑", node="keycloak", pillar="privilege", status="ok",
                      actor=requester, src=requester, dst="keycloak",
                      title=f"Keycloak gives {name(audience)} a pass for {user}",
                      caption=f"{name(requester)} trades its token for a narrower one. Only "
                              f"{name(audience)} will accept it, with just: "
                              f"{span.attr('token.scope') or 'no scopes'}.",
                      details=details)]
    return [_step(span, icon="⛔", node="keycloak", pillar="privilege", status="deny",
                  actor=requester, src=requester, dst="keycloak",
                  title=f"Keycloak refuses {name(requester)} → {name(audience)}",
                  caption=str(span.attr("token.reason")), details=details)]


def _tools_list(span: SpanRecord) -> list[Step]:
    tools = [t for t in str(span.attr("mcp.tools_visible")).split(",") if t]
    agent = span.attr("agent.id")
    return [_step(span, icon="🧰", node="ehr-mcp", pillar="trust", status="info", also=("opa",),
                  actor=agent, src=agent, dst="ehr-mcp",
                  title=f"{name(agent)} sees {len(tools)} EHR tools",
                  caption="The gateway lists only the tools policy lets this agent use: "
                          + (", ".join(tools) or "none") + ".",
                  details={"agent": agent, "visible tools": ", ".join(tools)})]


def _ehr_tool(span: SpanRecord) -> list[Step]:
    agent, tool = span.attr("agent.id"), span.attr("mcp.tool")
    details = {"agent": agent, "tool": tool, "for": span.attr("user.name"),
               "patient": span.attr("patient.id"), "policy": span.attr("policy.reason")}
    if span.attr("policy.decision") == "deny":
        return [_step(span, icon="🛡", node="opa", pillar="trust", status="deny",
                      actor=agent, src=agent, dst="ehr-mcp", also=("ehr-mcp",),
                      title=f"OPA blocks {name(agent)} → {tool}",
                      caption=f"Denied before any data was touched: {span.attr('policy.reason')}",
                      details=details)]
    withheld = int(span.attr("ehr.withheld", 0) or 0)
    caption = (f"OPA allowed it ({TOOL_PURPOSE.get(tool, tool)}). "
               f"{span.attr('ehr.returned', 0)} record(s) returned.")
    status = "ok"
    if withheld:
        status = "warn"
        caption += f" 🔒 {withheld} withheld: {span.attr('ehr.withheld_reasons')}."
        details["withheld"] = span.attr("ehr.withheld_reasons")
    if span.attr("ehr.break_glass") is True:
        status = "warn"
        caption += " ⚠ Unlocked by a break-the-glass grant (flagged for review)."
    return [_step(span, icon="🛠", node="ehr-mcp", pillar="trust", status=status,
                  actor=agent, src=agent, dst="ehr-mcp",
                  title=f"{name(agent)} calls {tool}", caption=caption, details=details,
                  also=("opa", agent))]


def _agent_start(span: SpanRecord) -> list[Step]:
    agent, task = span.attr("agent.id"), span.attr("agent.task")
    steps = [_step(span, icon="🤖", node=agent, pillar="agent", status="info",
                   actor=agent, src=str(span.attr("agent.called_by") or "orchestrator"),
                   dst=agent,
                   title=f"{name(agent)} starts: {task}",
                   caption=f"Called by the {name(span.attr('agent.called_by')).lower()} with a "
                           f"token addressed to the {name(agent).lower()} only.",
                   details={"reasoning": span.attr("agent.reasoning")})]
    for i, event in enumerate(span.events):
        a = event["attributes"]
        if event["name"] == "agent.misbehaving" and "submitted" in a:
            steps.append(_step(span, f"-ev{i}", icon="😈", node=agent, pillar="trust", actor=agent,
                               status="warn", title=f"{name(agent)} alters the signed prescription",
                               caption=f"Signed {a.get('approved')}, but it submits "
                                       f"{a.get('submitted')}. Will anything notice?",
                               details=a))
        elif event["name"] == "agent.misbehaving":
            steps.append(_step(span, f"-ev{i}", icon="😈", node=agent, pillar="trust", actor=agent,
                               status="warn", title=f"{name(agent)} goes rogue",
                               caption=f"It tries {a.get('tried')}, which its job never needs.",
                               details=a))
        elif event["name"] == "agent.guardrail":
            steps.append(_step(span, f"-ev{i}", icon="🛡", node=agent, pillar="trust",
                               actor=agent,
                               status="warn", title=f"Guardrail in the {name(agent).lower()}",
                               caption=str(a.get("detail", "")), details=a))
        elif event["name"] == "agent.decision":
            steps.append(_step(span, f"-ev{i}", icon="🎯", node=agent, pillar="accountability",
                               status="info", actor=agent,
                               title=f"{name(agent)} decided: {a.get('decision')}",
                               caption=f"Why: {a.get('rationale')}. {a.get('by', '')}. "
                                       "Recorded in the audit trail as agent.decision.",
                               details=a))
        elif event["name"] == "agent.blocked":
            steps.append(_step(span, f"-ev{i}", icon="🚫", node=agent, pillar="trust",
                               status="deny", actor=agent,
                               title=f"{name(agent)} was stopped",
                               caption=str(a.get("detail", "")), details=a))
        elif event["name"] == "agent.misled":
            steps.append(_step(span, f"-ev{i}", icon="🎣", node=agent, pillar="trust", actor=agent,
                               status="warn", title=f"{name(agent)} believes a portal message",
                               caption="Written like the patient, no trigger words, so no "
                                       "filter fires and nobody is warned. It asks for the "
                                       f"{a.get('asks')}. The agent tries; watch what stops it.",
                               details=a))
        elif event["name"] == "agent.oversharing":
            steps.append(_step(span, f"-ev{i}", icon="🙊", node=agent, pillar="trust", actor=agent,
                               status="warn", title=f"{name(agent)} over-shares",
                               caption=f"It attaches {a.get('attached')} records, including "
                                       "psychiatry notes, to a request to the payer.",
                               details=a))
    return steps


def _decide(span: SpanRecord) -> list[Step]:
    agent = span.attr("agent.id")
    what = span.name.split(": ", 1)[1]
    steps = [_step(span, icon="✍️", node=agent, pillar="agent", status="info", actor=agent,
                   title=f"{name(agent)}: {what}",
                   caption=f"Its own reasoning ({span.attr('agent.reasoning')}).")]
    for i, event in enumerate(span.events):
        a = event["attributes"]
        if event["name"] == "agent.finding":
            steps.append(_step(span, f"-f{i}", icon="⚠️", node=agent, pillar="agent", actor=agent,
                               status="warn" if a.get("severity") == "high" else "info",
                               title=f"Finding: {a.get('detail')}",
                               caption="Flagged for the physician to review before signing."))
        elif event["name"] == "security.injection_suspected":
            steps.append(_step(span, f"-inj{i}", icon="⚠️", node=agent, pillar="trust", actor=agent,
                               status="warn", title="Prompt injection spotted",
                               caption=f"{str(a.get('detail', '')).capitalize()}. Patient "
                                       "messages are data: the instructions were reported, "
                                       "not followed.", details=a))
    return steps


def _llm(span: SpanRecord) -> list[Step]:
    agent, task = span.attr("agent.id"), span.attr("llm.task")
    tools = [t for t in str(span.attr("llm.tools_used") or "").split(",") if t]
    skipped = [r for r in str(span.attr("llm.skipped") or "").split(" | ") if r]
    details = {"model": span.attr("llm.label"), "endpoint": span.attr("llm.endpoint"),
               "tokens in / out": f"{span.attr('gen_ai.usage.input_tokens', 0)} / "
                                  f"{span.attr('gen_ai.usage.output_tokens', 0)}",
               "seconds": span.attr("llm.seconds"), "tools chosen": ", ".join(tools),
               "skipped": "; ".join(skipped)}
    steps = []
    for i, event in enumerate(span.events):
        a = event["attributes"]
        if event["name"] == "llm.fallback":
            steps.append(_step(span, f"-fb{i}", icon="↪️", node=agent, pillar="agent", actor=agent,
                               status="warn", title=f"Model unavailable: {a.get('provider')}",
                               caption=f"{a.get('reason')}. Trying the next option.", details=a))
        elif event["name"] == "agent.guardrail":
            steps.append(_step(span, f"-g{i}", icon="🛡", node=agent, pillar="trust", actor=agent,
                               status="warn", title=f"Guardrail in the {name(agent).lower()}",
                               caption=str(a.get("detail", "")), details=a))
    if span.attr("llm.used_model") is True:
        caption = f"{span.attr('llm.label')} {task}"
        if tools:
            caption += (f"; it chose {', '.join(tools)}. The model holds no permissions: each "
                        "call it picks goes through the EHR gateway and OPA like any other.")
        else:
            caption += ("; it only sees facts the agent chose to give it (labelled records "
                        "were removed first).")
        steps.insert(0, _step(span, icon="✨", node="model", pillar="agent", status="info",
                              actor=agent, src=agent, dst="model", also=(agent,),
                              title=f"{name(agent)} asks the model: {task}", caption=caption,
                              details=details))
    else:
        steps.append(_step(span, "-rules", icon="⚙", node=agent, pillar="agent", status="info",
                           actor=agent,
                           title=f"{name(agent)} falls back to rules",
                           caption=f"No model answered ({skipped[-1] if skipped else 'none'}). "
                                   "Deterministic rules write it instead.",
                           details=details))
    return steps


def _disclosure(span: SpanRecord) -> list[Step]:
    agent, partner = span.attr("agent.id"), span.attr("disclosure.destination")
    purpose = str(span.attr("disclosure.purpose")).replace("_", " ")
    details = {"agent": agent, "for": span.attr("user.name"), "to": partner,
               "purpose": purpose, "requested": span.attr("disclosure.requested_fields"),
               "sent": span.attr("disclosure.sent_fields")}
    if span.attr("policy.decision") == "deny":
        return [_step(span, icon="⛔", node="disclosure-mcp", pillar="trust", status="deny",
                      also=("opa",), actor=agent, src=agent, dst="disclosure-mcp",
                      title=f"Disclosure to {PARTNER_NAMES.get(partner, partner)} blocked",
                      caption=str(span.attr("policy.reason")), details=details)]
    stripped = int(span.attr("disclosure.stripped", 0) or 0)
    caption = (f"For {purpose}: OPA allows {span.attr('disclosure.sent_fields')}. "
               "Sent with a token addressed to this partner only.")
    if stripped:
        caption += f" 🛡 Withheld {stripped}: {span.attr('disclosure.stripped_reasons')}."
    if span.attr("partner.status") not in ("", None, 200):
        caption += f" Partner answered HTTP {span.attr('partner.status')}."
    return [_step(span, icon="📤", node="disclosure-mcp", pillar="trust",
                  status="warn" if stripped else "ok", also=("opa", partner),
                  actor=agent, src=agent, dst="disclosure-mcp",
                  title=f"{name(agent)} → {PARTNER_NAMES.get(partner, partner)}: {purpose}",
                  caption=caption, details=details)]


def _partner(span: SpanRecord) -> list[Step]:
    partner = span.attr("partner.name")
    return [_step(span, icon="🏥", node=partner, pillar="trust", status="info",
                  actor=str(span.attr("partner.sent_by") or partner), src="disclosure-mcp",
                  dst=partner,
                  title=f"{PARTNER_NAMES.get(partner, partner)} received it",
                  caption=f"Outside the hospital now: {span.attr('partner.fields')}. The partner "
                          "checked the token was addressed to it.",
                  details={"fields": span.attr("partner.fields")})]


def _approval(span: SpanRecord) -> list[Step]:
    action = span.name.split(" ", 1)[1]
    allowed = span.attr("policy.decision") == "allow"
    reason = span.attr("policy.reason")
    titles = {
        ("create", True): ("✍", "Draft placed in the physician's signature queue",
                           "Agents can request a signature but never sign. The content is "
                           "fingerprinted now; whatever is signed is exactly what may be sent."),
        ("consume", True): ("✅", "Signature verified: content matches",
                            "What is being sent matches the fingerprint the physician signed. "
                            "The signature is now used up."),
        ("consume", False): ("🛡", "Signature check failed: nothing sent", reason),
        ("decide", True): ("🩺", "Physician's decision recorded", reason),
        ("decide", False): ("🛡", "Signature refused", reason),
    }
    icon, title, caption = titles.get((action, allowed),
                                      ("🛡" if not allowed else "✅", f"Approval: {action}",
                                       reason))
    return [_step(span, icon=icon, node="approvals", pillar="trust", also=("opa",),
                  actor="approvals", dst="approvals",
                  status="ok" if allowed else "deny", title=title, caption=caption,
                  details={"policy": "hospital/approval", "decision": reason})]


def _audit(span: SpanRecord) -> list[Step]:
    decision = span.attr("audit.decision")
    writer = str(span.attr("audit.writer") or "")
    return [_step(span, icon="📝", node="audit", pillar="accountability",
                  actor=writer or "audit", src=writer, dst="audit",
                  status="deny" if decision == "deny" else "info",
                  title=f"Audit #{span.attr('audit.seq')}: {span.attr('audit.action')}",
                  caption=f"Recorded as {decision.upper()} in the hash-chained log, written by "
                          f"{name(span.attr('audit.writer'))}.",
                  details={"hash": str(span.attr("audit.hash"))[:16] + "…"})]


def _security_alert(span: SpanRecord) -> list[Step]:
    agent = span.attr("alert.agent")
    return [_step(span, icon="🚨", node="orchestrator", pillar="accountability", status="deny",
                  actor="orchestrator", src=agent, dst="orchestrator", also=(agent,),
                  title=f"Security alert: {name(agent)} suspended",
                  caption=f"The EHR gateway's own audit entry #{span.attr('alert.audit_seq')} "
                          f"shows it was blocked from {span.attr('alert.tool')} "
                          f"({span.attr('alert.reason')}). The coordinator stops trusting "
                          "it: its result is not used and the discharge is on hold until a "
                          "physician reviews.",
                  details={"blocked": span.attr("alert.tool"),
                           "reason": span.attr("alert.reason"),
                           "gateway audit entry": span.attr("alert.audit_seq")})]


# Which spans are story steps. Mirrored by transform/story in otel-collector.yaml.
_RULES = (
    (lambda s: s.name in ("ui.action", "ui.ask"), _ask),
    (lambda s: s.name == "orchestrator plan", _plan),
    (lambda s: s.name == "orchestrator security alert", _security_alert),
    (lambda s: s.name.startswith("token_exchange ") and s.attr("token.requester"), _exchange),
    (lambda s: s.name == "tools/list" and s.attr("mcp.tools_visible", None) is not None
     and s.service == "ehr-mcp", _tools_list),
    (lambda s: s.name.startswith("ehr-mcp ") and s.attr("mcp.tool"), _ehr_tool),
    (lambda s: s.name.startswith("disclosure-mcp ") and s.attr("disclosure.purpose"),
     _disclosure),
    (lambda s: s.service == "partners" and s.attr("partner.name"), _partner),
    (lambda s: s.name.startswith("llm ") and s.attr("llm.task"), _llm),
    (lambda s: s.name.startswith("agent ") and s.attr("agent.task"), _agent_start),
    (lambda s: s.name.startswith("agent ") and ": " in s.name and s.attr("agent.id"), _decide),
    (lambda s: s.service == "approval-service" and s.name.startswith("policy ")
     and s.name != "policy view", _approval),
    (lambda s: s.attr("audit.seq", None) is not None
     and s.attr("audit.action") in STORY_AUDIT_ACTIONS, _audit),
)
