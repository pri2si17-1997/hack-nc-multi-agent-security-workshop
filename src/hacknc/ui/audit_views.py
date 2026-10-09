"""Audit entries shaped for people: the chart's access log, the accounting of disclosures,
and what each partner says it received. Pure functions (no I/O), so they are easy to test."""

from __future__ import annotations

from typing import Any

from hacknc.common.flags import ATTACK_STYLES


def disclosure_rows(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Audit entries -> the 'what left the hospital' view."""
    rows = []
    for e in entries:
        d = e.get("details", {})
        rows.append({"at": e["recorded_at"], "seq": e["seq"], "patient_id": e.get("patient_id"),
                     "destination": d.get("destination_name", d.get("destination")),
                     "partner": d.get("destination"),
                     "chain": e.get("agent_chain") or [],
                     "purpose": (d.get("purpose") or "").replace("_", " "),
                     "fields": d.get("fields", []), "withheld": d.get("withheld", []),
                     "agent": (e.get("agent_chain") or ["?"])[0], "for": e.get("actor_user"),
                     "decision": e["decision"], "reason": e["reason"],
                     "trace_id": e.get("trace_id")})
    return rows


def attach_payloads(rows: list[dict[str, Any]],
                    received: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add what each partner says it received to the hospital's own disclosure rows.

    Partners log every request they accept (some are refused right after, e.g. a pharmacy
    whose signature check fails), and partners can be reset while the audit chain cannot.
    So rows and receipts are paired per partner from the newest backwards. Pass the rows of
    *every* patient in the period, or the pairing shifts."""
    reached = [r for r in rows if r["decision"] == "allow" or " refused: " in r["reason"]]
    for partner in {r["partner"] for r in reached}:
        mine = sorted((r for r in reached if r["partner"] == partner), key=lambda r: r["seq"])
        theirs = [x for x in received if x.get("partner") == partner]
        for row, receipt in zip(reversed(mine), reversed(theirs), strict=False):
            row["payload"] = receipt.get("body")
            row["receipt"] = {k: receipt.get(k) for k in ("at", "fields", "sent_by",
                                                          "on_behalf_of")}
    return rows


ACCESS_LABELS = {
    "mcp.tool_call": "read", "signature.requested": "asked for a signature",
    "portal.post": "posted to the portal", "encounter.discharged": "recorded the discharge",
    "break_glass.access": "emergency access", "disclosure.sent": "disclosed",
    "disclosure.blocked": "tried to disclose", "agent.decision": "decided",
    "agent.blocked": "was stopped", "model.invoked": "asked the model",
    "discharge.started": "ordered the discharge", "discharge.refused": "was refused a discharge",
    "discharge.reopened": "reopened the discharge", "coordinator.step": "coordinated",
    "approval.approve": "signed", "approval.reject": "rejected",
    "approval.request": "asked for a signature", "rx.fulfilled": "used a signature",
    "portal.message": "added a portal message", "patient_request.submitted": "filed a request",
    "patient_request.decided": "decided a patient request", "consent.changed": "changed consent",
    "security.alert": "raised a security alert",
    "security.alert_acknowledged": "reviewed a security alert",
    "break_glass.granted": "was granted emergency access",
    "scenario.staged": "had a workshop attack staged",
}


def attack_notes(entries: list[dict[str, Any]],
                 markers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Label what a staged workshop attack caused, so the room can find it in the trail.

    ``markers`` are the ``scenario.staged`` entries agents write when an attack is staged.
    One user request is one trace across every service, so a marker covers the marker itself
    and every refusal or withholding *by that agent* in the same trace: the agent's own
    ``agent.blocked`` and the gateways' and OPA's denials (their chains start with the agent).
    Allowed, ordinary steps of the same run are not labelled."""
    staged: dict[tuple[str, str], str] = {}
    for m in markers:
        agent = (m.get("agent_chain") or [None])[-1]
        scenario = (m.get("details") or {}).get("scenario")
        if m.get("trace_id") and agent and scenario:
            staged[(m["trace_id"], agent)] = scenario
    for e in entries:
        details = e.get("details") or {}
        scenario = details.get("scenario") if e["action"] == "scenario.staged" else None
        if scenario is None and (e["decision"] == "deny" or details.get("withheld")):
            scenario = next((sc for (trace, agent), sc in staged.items()
                             if trace == e.get("trace_id")
                             and agent in (e.get("agent_chain") or [])), None)
        if scenario:
            label, style = ATTACK_STYLES.get(scenario, (scenario, ""))
            e["attack"] = {"scenario": scenario, "label": label, "style": style}
    return entries


def access_rows(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Audit entries about one patient -> the chart's access log (newest first)."""
    rows = []
    for e in entries:
        chain = e.get("agent_chain") or []
        rows.append({"seq": e["seq"], "at": e["recorded_at"], "who": e.get("actor_user"),
                     "role": e.get("actor_role"), "via": chain, "writer": e.get("writer"),
                     "what": ACCESS_LABELS.get(e["action"], e["action"]),
                     "action": e["action"], "reason": e["reason"],
                     "decision": e["decision"], "severity": e.get("severity"),
                     "trace_id": e.get("trace_id")})
    return sorted(rows, key=lambda r: r["seq"], reverse=True)
