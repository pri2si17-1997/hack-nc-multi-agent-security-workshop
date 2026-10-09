"""The incident report: "Prove what your AI agents did with this patient's data."

Built only from the hash-chained audit trail, the partners' own logs and the chain check.
No agent is asked what it did (a rogue agent would not tell). It answers the five questions
a regulator asks, for one patient over one period:

    who        Who acted?             every person and agent, in order of first appearance
    authority  On whose authority?    the person, the agent chain, token scopes, the policy
    seen       What did they see?     records read, records withheld and why, emergency access
    left       What left the hospital? each disclosure next to what the partner says it got
    stopped    What was stopped?      blocked attempts, the alert, who reviewed it, and OPA's
                                      own decision log as an independent witness

Plus ``integrity`` (is the record itself intact?) and a ``timeline`` of the key events.
Everything here is a pure function of its inputs, so it is easy to test.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from hacknc.ui.audit_views import ACCESS_LABELS, attach_payloads, disclosure_rows
from hacknc.ui.presenters import AGENT_LABELS

HOSPITAL_APP = "hospital-ui"
WITNESS = "opa"                       # OPA's decision log, written by OPA itself
PERIOD_MARKS = ("demo.reset", "demo.finale_prepared")
DISCLOSURES = ("disclosure.sent", "disclosure.blocked")
KEY_ACTIONS = {
    "portal.message", "patient_request.submitted", "patient_request.decided", "consent.changed",
    "discharge.started", "discharge.reopened", "discharge.refused", "agent.decision",
    "agent.blocked", "security.alert", "security.alert_acknowledged", "signature.requested",
    "approval.approve", "approval.reject", "encounter.discharged", "break_glass.access",
    "break_glass.granted", *DISCLOSURES,
}

Entry = dict[str, Any]


# --------------------------------------------------------------------------- small helpers


def actor_of(entry: Entry) -> str:
    """Who acted: an agent writing its own decision, else the first hop of the agent chain
    (``hospital-ui`` means a person acting directly in the app)."""
    writer = entry.get("writer") or ""
    if writer.endswith("-agent"):
        return writer
    chain = entry.get("agent_chain") or []
    return chain[0] if chain else writer


def is_agent(name: str) -> bool:
    return name == "orchestrator" or name.endswith("-agent")


def label_of(name: str) -> str:
    if name == HOSPITAL_APP:
        return "Hospital app"
    return AGENT_LABELS.get(name, name)


def refused(entry: Entry) -> bool:
    """Was this party's own attempt refused? (A raised alert is recorded as a deny too, but
    there the coordinator is the one doing the stopping.)"""
    return entry["decision"] == "deny" and entry["action"] != "security.alert"


def what(entry: Entry) -> str:
    if entry["action"] == "mcp.tool_call" and entry["decision"] == "deny":
        return "was blocked"
    return ACCESS_LABELS.get(entry["action"], entry["action"])


def patients_in(entries: list[Entry]) -> list[dict[str, Any]]:
    """Patients the period's entries are about, most mentioned first (for the picker)."""
    counts = Counter(e["patient_id"] for e in entries
                     if e.get("patient_id") and e.get("writer") != WITNESS)
    return [{"id": pid, "entries": n} for pid, n in counts.most_common()]


def period_start(marks: list[Entry]) -> Entry | None:
    """The newest demo reset (or finale preparation): the default start of the report."""
    found = [m for m in marks if m.get("action") in PERIOD_MARKS]
    return max(found, key=lambda m: m["seq"]) if found else None


def _brief(entry: Entry, altered: int | None) -> dict[str, Any]:
    actor = actor_of(entry)
    return {"seq": entry["seq"], "at": entry["recorded_at"], "who": entry.get("actor_user"),
            "role": entry.get("actor_role"), "actor": actor, "actor_label": label_of(actor),
            "what": what(entry), "action": entry["action"], "decision": entry["decision"],
            "severity": entry.get("severity"), "reason": entry.get("reason", ""),
            "chain": entry.get("agent_chain") or [], "trace_id": entry.get("trace_id"),
            "altered": entry["seq"] == altered}


# --------------------------------------------------------------------------- the report


def build_report(patient_id: str, entries: list[Entry], received: list[Entry],
                 chain: dict[str, Any] | None, since: Entry | None = None,
                 chain_problem: str = "") -> dict[str, Any]:
    """``entries``: every audit entry of the period (all patients, all writers).
    ``received``: the partners' own logs. ``chain``: the audit service's verify result
    (None if it could not be checked; ``chain_problem`` says why)."""
    entries = sorted(entries, key=lambda e: e["seq"])
    by_seq = {e["seq"]: e for e in entries}
    altered = chain.get("broken_at") if chain and not chain.get("ok") else None

    alerts = [e for e in entries if e["action"] == "security.alert"
              and e.get("patient_id") == patient_id]
    # The blocked attempt may be about *another* patient (the agent reached for it): the
    # alert names the gateway's entry, so it belongs to this patient's report.
    referenced = {(a.get("details") or {}).get("gateway_audit_seq") for a in alerts}
    mine = [e for e in entries if e.get("writer") != WITNESS
            and (e.get("patient_id") == patient_id or e["seq"] in referenced)]
    witnesses = [e for e in entries if e.get("writer") == WITNESS]

    left = _left(patient_id, entries, received, alerts)
    stopped = _stopped(mine, alerts, by_seq, witnesses, altered)
    seen = _seen(mine, patient_id, altered)
    who = _who(mine)
    return {
        "patient_id": patient_id,
        "period": {
            "since_seq": since["seq"] if since else None,
            "since_at": since["recorded_at"] if since else None,
            "since_what": (since.get("reason") or since["action"]) if since else None,
            "first_at": mine[0]["recorded_at"] if mine else None,
            "last_at": mine[-1]["recorded_at"] if mine else None,
            "entries": len(mine),
        },
        "integrity": _integrity(chain, chain_problem, {e["seq"] for e in mine}),
        "headline": {
            "people": sum(1 for w in who if w["kind"] == "person"),
            "agents": sum(1 for w in who if w["kind"] == "agent"),
            "records_read": sum(r["returned"] for r in seen["reads"]),
            "withheld": sum(len(w["ids"]) for w in seen["withheld"]),
            "sent": sum(1 for d in left if d["decision"] == "allow"),
            "stopped": len(stopped["blocked"]) + len(stopped["refused"]),
        },
        "who": who,
        "authority": _authority(mine),
        "seen": seen,
        "left": left,
        "stopped": stopped,
        "system": [_brief(e, altered) for e in entries if e["action"].startswith("demo.")],
        "timeline": [_brief(e, altered) for e in mine
                     if (e["action"] in KEY_ACTIONS and _not_duplicate(e))
                     or e["decision"] == "deny" or e["seq"] == altered],
    }


def _not_duplicate(entry: Entry) -> bool:
    # The gateway and the coordinator both record a patient-request decision; keep one.
    return not (entry["action"] == "patient_request.decided" and entry["writer"] == "ehr-mcp")


def _integrity(chain: dict[str, Any] | None, problem: str, seqs: set[int]) -> dict[str, Any]:
    if chain is None:
        return {"ok": None, "problem": problem or "the audit service could not be reached"}
    broken = chain.get("broken_at")
    return {"ok": bool(chain.get("ok")), "checked": chain.get("checked", 0),
            "broken_at": broken, "problem": chain.get("problem", ""),
            "head": (chain.get("head_hash") or "")[:16],
            "in_report": broken in seqs if broken else False}


def _who(mine: list[Entry]) -> list[dict[str, Any]]:
    """Every person and agent, in order of first appearance, with what they did. A person
    acting in the hospital app did it directly; an agent always acts *for* a person."""
    parties: dict[str, dict[str, Any]] = {}

    def party(name: str, kind: str, e: Entry) -> dict[str, Any]:
        return parties.setdefault(name, {
            "name": name, "kind": kind, "label": label_of(name) if kind == "agent" else name,
            "role": None, "first_at": e["recorded_at"], "first_seq": e["seq"],
            "last_at": e["recorded_at"], "for": [], "did": Counter(), "blocked": 0,
            "through_agents": 0})

    for e in mine:
        actor, user = actor_of(e), e.get("actor_user")
        person = party(user, "person", e) if user else None
        if person is not None:
            person["role"] = person["role"] or e.get("actor_role")
            person["last_at"] = e["recorded_at"]
        doer = party(actor, "agent", e) if is_agent(actor) else person
        if doer is None:
            continue
        doer["last_at"] = e["recorded_at"]
        doer["did"][what(e)] += 1
        doer["blocked"] += refused(e)
        if doer is not person and person is not None:
            person["through_agents"] += 1
            if user not in doer["for"]:
                doer["for"].append(user)
    for p in parties.values():
        p["did"] = [{"what": k, "count": n} for k, n in p["did"].most_common()]
    return sorted(parties.values(), key=lambda p: p["first_seq"])


def _authority(mine: list[Entry]) -> list[dict[str, Any]]:
    """Per acting party and person: the agent chains, scopes and policies behind the actions."""
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for e in mine:
        actor, user = actor_of(e), e.get("actor_user") or "—"
        group = groups.setdefault((actor, user), {
            "actor": actor, "actor_label": label_of(actor), "for": user,
            "role": e.get("actor_role"), "chains": [], "scopes": set(), "policies": set(),
            "decisions": Counter(), "first_seq": e["seq"]})
        group["role"] = group["role"] or e.get("actor_role")
        chain = " → ".join(e.get("agent_chain") or [])
        if chain and chain not in group["chains"]:
            group["chains"].append(chain)
        group["scopes"].update((e.get("details") or {}).get("scopes") or [])
        if e.get("policy"):
            group["policies"].add(e["policy"])
        group["decisions"]["deny" if refused(e) else "ok"] += 1
    rows = []
    for g in sorted(groups.values(), key=lambda g: g["first_seq"]):
        rows.append({**g, "scopes": sorted(g["scopes"]), "policies": sorted(g["policies"]),
                     "actions": sum(g["decisions"].values()),
                     "denied": g["decisions"].get("deny", 0),
                     "direct": g["actor"] == HOSPITAL_APP or not is_agent(g["actor"])})
    return rows


def _seen(mine: list[Entry], patient_id: str, altered: int | None) -> dict[str, Any]:
    reads: dict[tuple[str, str], dict[str, Any]] = {}
    withheld, emergency = [], []
    for e in mine:
        details = e.get("details") or {}
        if e["action"].startswith("break_glass."):
            emergency.append(_brief(e, altered))
        if e.get("patient_id") != patient_id or e["decision"] != "allow" or "tool" not in details:
            continue
        if e["action"] == "mcp.tool_call":
            actor = actor_of(e)
            read = reads.setdefault((actor, e.get("actor_user") or "—"), {
                "actor": actor, "actor_label": label_of(actor), "for": e.get("actor_user"),
                "tools": Counter(), "returned": 0, "calls": 0})
            read["tools"][details["tool"]] += 1
            read["returned"] += int(details.get("returned") or 0)
            read["calls"] += 1
        if details.get("withheld"):
            withheld.append({**_brief(e, altered), "tool": details["tool"],
                             "ids": list(details["withheld"])})
    return {
        "reads": [{**r, "tools": [{"tool": t, "count": n} for t, n in r["tools"].most_common()]}
                  for r in reads.values()],
        "withheld": withheld,
        "emergency": emergency,
    }


def _left(patient_id: str, entries: list[Entry], received: list[Entry],
          alerts: list[Entry]) -> list[dict[str, Any]]:
    """This patient's disclosures, each with what the partner logged on its side."""
    rows = disclosure_rows([e for e in entries if e["action"] in DISCLOSURES])
    attach_payloads(rows, received)       # pair with every patient's rows, then keep ours
    # (agent, trace, alert seq) of each suspended task: what it sent before the alert was
    # already out. The gateway checked each disclosure, but the report says so plainly.
    suspended_tasks = [((a.get("agent_chain") or [None])[-1], a.get("trace_id"), a["seq"])
                       for a in alerts]
    mine = []
    for row in sorted((r for r in rows if r["patient_id"] == patient_id),
                      key=lambda r: r["seq"]):
        receipt = row.get("receipt")
        row["agrees"] = (sorted(receipt.get("fields") or []) == sorted(row["fields"])
                         if receipt and row["decision"] == "allow" else None)
        row["during_suspended_task"] = any(
            agent == row["agent"] and trace and trace == row["trace_id"] and row["seq"] < seq
            for agent, trace, seq in suspended_tasks)
        mine.append(row)
    return mine


def _stopped(mine: list[Entry], alerts: list[Entry], by_seq: dict[int, Entry],
             witnesses: list[Entry], altered: int | None) -> dict[str, Any]:
    """Blocked attempts (with alert, review and witness) and every other refusal."""
    acks = [e for e in mine if e["action"] == "security.alert_acknowledged"]
    used_witnesses: set[int] = set()
    blocked, linked = [], set()
    for alert in alerts:
        gateway_seq = (alert.get("details") or {}).get("gateway_audit_seq")
        attempt = by_seq.get(gateway_seq)
        agent = (alert.get("agent_chain") or [None])[-1]
        linked.update({alert["seq"], gateway_seq})
        review = next((a for a in acks
                       if (a.get("details") or {}).get("gateway_audit_seq") == gateway_seq),
                      None) or next((a for a in acks if a["seq"] > alert["seq"]
                                     and agent and agent in a.get("reason", "")), None)
        witness = None
        if attempt is not None:
            witness = next((w for w in witnesses if w["seq"] not in used_witnesses
                            and w["decision"] == "deny"
                            and w.get("patient_id") == attempt.get("patient_id")
                            and actor_of(w) == actor_of(attempt)), None)
            if witness is not None:
                used_witnesses.add(witness["seq"])
        blocked.append({
            "alert": _brief(alert, altered),
            "agent": agent, "agent_label": label_of(agent or ""),
            "attempt": _brief(attempt, altered) if attempt else None,
            "attempted_tool": ((attempt or {}).get("details") or {}).get("tool"),
            "other_patient": (attempt or {}).get("patient_id"),
            "missing": attempt is None,
            # After tampering, the gateway's own entry may no longer say it blocked anything.
            "consistent": attempt is not None and attempt["decision"] == "deny",
            "review": _brief(review, altered) if review else None,
            "witness": _brief(witness, altered) if witness else None,
        })
    refused = [_brief(e, altered) for e in mine
               if e["decision"] == "deny" and e["seq"] not in linked]
    flagged = [_brief(e, altered) for e in mine
               if e["decision"] != "deny" and _flagged(e.get("reason", ""))]
    return {"blocked": blocked, "refused": refused, "flagged": flagged}


def _flagged(reason: str) -> bool:
    """Allowed, but marked as suspicious on the way (e.g. a prompt injection in the portal)."""
    return "injection" in reason.lower() or "⚠ flagged" in reason
