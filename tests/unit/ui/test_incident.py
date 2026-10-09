"""The incident report answers the five questions from audit entries and partner logs only."""

from __future__ import annotations

from typing import Any

import pytest

from hacknc.ui import incident

JANE, KERMIT = "jane-doe", "pt-87c46349"
TRACE = "t-night"


def entry(seq: int, action: str, writer: str, chain: list[str], *, user: str | None = "dr.morgana",
          role: str | None = "physician", patient: str | None = JANE, decision: str = "info",
          severity: str = "info", reason: str = "", details: dict[str, Any] | None = None,
          policy: str | None = None, trace: str | None = TRACE) -> dict[str, Any]:
    return {"seq": seq, "recorded_at": f"2026-10-04T21:00:{seq:02d}.000+00:00",
            "writer": writer, "action": action, "decision": decision, "severity": severity,
            "actor_user": user, "actor_role": role, "agent_chain": chain, "patient_id": patient,
            "reason": reason, "details": details or {}, "policy": policy, "trace_id": trace,
            "hash": f"h{seq}", "prev_hash": f"h{seq - 1}"}


def gateway_read(seq: int, agent: str, tool: str, returned: int, withheld=(), user="dr.morgana",
                 role="physician", patient=JANE, decision="allow", reason="") -> dict[str, Any]:
    return entry(seq, "mcp.tool_call", "ehr-mcp", [agent, "ehr-mcp"], user=user, role=role,
                 patient=patient, decision=decision,
                 severity="warning" if decision == "deny" else "info",
                 reason=reason or f"{tool}: {returned} returned",
                 policy="hospital/ehr/tool_decision",
                 details={"tool": tool, "returned": returned, "withheld": list(withheld),
                          "scopes": ["patient:read"]})


def disclosure(seq: int, agent: str, partner: str, name: str, purpose: str,
               fields: list[str], patient: str = JANE) -> dict[str, Any]:
    return entry(seq, "disclosure.sent", "disclosure-mcp", [agent, "disclosure-mcp", partner],
                 decision="allow", patient=patient, reason=f"sent {', '.join(fields)}",
                 policy="hospital/disclosure/decision",
                 details={"destination": partner, "destination_name": name, "purpose": purpose,
                          "fields": fields, "withheld": [], "scopes": ["disclose:payer"]})


NIGHT = [
    entry(1, "demo.finale_prepared", "hospital-ui", ["hospital-ui"], user="admin.percival",
          role="admin", patient=None, reason="finale prepared", trace=None),
    entry(2, "portal.message", "ehr-mcp", ["portal-agent", "ehr-mcp"], user="jane.doe",
          role="patient", severity="warning", decision="allow",
          reason="patient portal message added to the chart; ⚠ flagged: instructions aimed "
                 "at AI assistants", details={"tool": "post_portal_message", "returned": 1}),
    entry(3, "discharge.started", "orchestrator", ["hospital-ui", "orchestrator"],
          reason="discharge ordered in chat"),
    gateway_read(4, "medrec-agent", "get_medications", 8),
    gateway_read(5, "medrec-agent", "get_clinical_notes", 3, withheld=["ov-jd-note-psych"],
                 reason="get_clinical_notes: 3 returned, 1 withheld (PSY)"),
    gateway_read(6, "benefits-agent", "get_coverage", 0, patient=KERMIT, decision="deny",
                 reason="benefits-agent is not assigned to patient pt-87c46349"),
    disclosure(7, "benefits-agent", "payer", "Blue Cross Blue Shield", "prior_authorization",
               ["dob", "name", "service"]),
    entry(8, "security.alert", "orchestrator", ["orchestrator", "benefits-agent"],
          role=None, decision="deny", severity="high",
          reason="benefits-agent suspended: gateway blocked get_coverage",
          details={"task": "benefits", "gateway_audit_seq": 6}),
    entry(9, "policy.decision", "opa", ["benefits-agent"], patient=KERMIT, decision="deny",
          reason="benefits-agent is not assigned to patient pt-87c46349", trace=None),
    entry(10, "demo.control_changed", "hospital-ui", ["hospital-ui"], user="admin.percival",
          role="admin", patient=None, reason="scenario.wrong_patient=off", trace="t2"),
    entry(11, "security.alert_acknowledged", "orchestrator", ["hospital-ui", "orchestrator"],
          reason="dr.morgana reviewed: benefits-agent ...; task retried", trace="t3",
          details={"alert_id": "alert-6", "gateway_audit_seq": 6}),
    disclosure(12, "benefits-agent", "payer", "Blue Cross Blue Shield", "prior_authorization",
               ["dob", "name", "service"]) | {"trace_id": "t3"},
    entry(13, "approval.approve", "approval-service", ["hospital-ui"], decision="allow",
          reason="dr.morgana approved the request", trace="t4"),
    disclosure(14, "transitions-agent", "pcp-clinic", "Maple Family Medicine",
               "care_transition", ["dob", "name", "summary"]) | {"trace_id": "t4"},
    disclosure(15, "scheduling-agent", "pcp-clinic", "Maple Family Medicine", "appointment",
               ["dob", "name"], patient="pt-other"),
    entry(16, "discharge.refused", "orchestrator", ["hospital-ui", "orchestrator"],
          user="nurse.gawain", role="nurse", decision="deny", severity="warning",
          reason="nurse.gawain may not order a discharge"),
]
RECEIVED = [
    {"partner": "payer", "at": "21:00:07", "fields": ["dob", "name", "service"],
     "sent_by": "disclosure-mcp", "on_behalf_of": "dr.morgana", "body": {"name": "Jane Doe"}},
    {"partner": "payer", "at": "21:00:12", "fields": ["dob", "name", "service"],
     "sent_by": "disclosure-mcp", "on_behalf_of": "dr.morgana", "body": {"name": "Jane Doe"}},
    {"partner": "pcp-clinic", "at": "21:00:14", "fields": ["dob", "name", "summary", "extra"],
     "sent_by": "disclosure-mcp", "on_behalf_of": "dr.morgana", "body": {"name": "Jane Doe"}},
    {"partner": "pcp-clinic", "at": "21:00:15", "fields": ["dob", "name"],
     "sent_by": "disclosure-mcp", "on_behalf_of": "dr.morgana", "body": {"name": "Someone"}},
]
INTACT = {"ok": True, "checked": 16, "broken_at": None, "problem": "",
          "head_hash": "f" * 64}


@pytest.fixture
def report() -> dict[str, Any]:
    return incident.build_report(JANE, NIGHT, RECEIVED, INTACT, since=NIGHT[0])


def test_who_acted_in_order_people_and_agents(report) -> None:
    order = [(w["kind"], w["name"]) for w in report["who"]]
    assert order == [("person", "jane.doe"), ("agent", "portal-agent"),
                     ("person", "dr.morgana"), ("agent", "medrec-agent"),
                     ("agent", "benefits-agent"), ("agent", "orchestrator"),
                     ("agent", "transitions-agent"), ("person", "nurse.gawain")]
    benefits = next(w for w in report["who"] if w["name"] == "benefits-agent")
    assert benefits["for"] == ["dr.morgana"] and benefits["blocked"] == 1
    assert benefits["label"] == "Benefits & prior auth"
    coordinator = next(w for w in report["who"] if w["name"] == "orchestrator")
    assert coordinator["blocked"] == 0          # raising an alert is not being blocked
    doctor = next(w for w in report["who"] if w["name"] == "dr.morgana")
    assert doctor["role"] == "physician" and doctor["through_agents"] >= 5
    assert {"what": "signed", "count": 1} in doctor["did"]
    assert "admin.percival" not in order             # system changes are listed separately


def test_authority_names_chain_scopes_and_policy(report) -> None:
    benefits = next(a for a in report["authority"] if a["actor"] == "benefits-agent")
    assert benefits["for"] == "dr.morgana" and benefits["role"] == "physician"
    assert "benefits-agent → ehr-mcp" in benefits["chains"]
    assert "benefits-agent → disclosure-mcp → payer" in benefits["chains"]
    assert benefits["scopes"] == ["disclose:payer", "patient:read"]
    assert benefits["policies"] == ["hospital/disclosure/decision", "hospital/ehr/tool_decision"]
    assert benefits["denied"] == 1 and not benefits["direct"]
    app = next(a for a in report["authority"] if a["actor"] == "hospital-ui"
               and a["for"] == "dr.morgana")
    assert app["direct"] and app["actor_label"] == "Hospital app"


def test_what_they_saw_and_what_was_withheld(report) -> None:
    reads = {r["actor"]: r for r in report["seen"]["reads"]}
    assert reads["medrec-agent"]["returned"] == 11 and reads["medrec-agent"]["calls"] == 2
    assert "benefits-agent" not in reads        # its only gateway call was refused
    [withheld] = report["seen"]["withheld"]
    assert withheld["ids"] == ["ov-jd-note-psych"] and "PSY" in withheld["reason"]
    assert report["headline"]["records_read"] == 11 and report["headline"]["withheld"] == 1


def test_what_left_matches_the_partners_logs(report) -> None:
    left = {d["seq"]: d for d in report["left"]}
    assert set(left) == {7, 12, 14}             # another patient's appointment is not here
    assert left[7]["agrees"] and left[12]["agrees"]
    assert left[14]["agrees"] is False          # the clinic logged a field we didn't record
    assert left[7]["during_suspended_task"]     # sent before the agent was suspended
    assert not left[12]["during_suspended_task"]
    assert left[7]["payload"] == {"name": "Jane Doe"}
    assert report["headline"]["sent"] == 3


def test_what_was_stopped_with_review_and_witness(report) -> None:
    [blocked] = report["stopped"]["blocked"]
    assert blocked["agent"] == "benefits-agent" and blocked["attempted_tool"] == "get_coverage"
    assert blocked["other_patient"] == KERMIT and blocked["consistent"]
    assert blocked["review"]["who"] == "dr.morgana" and blocked["review"]["seq"] == 11
    assert blocked["witness"]["seq"] == 9 and blocked["witness"]["actor"] == "benefits-agent"
    [refused] = report["stopped"]["refused"]
    assert refused["action"] == "discharge.refused" and refused["who"] == "nurse.gawain"
    assert [f["seq"] for f in report["stopped"]["flagged"]] == [2]
    assert report["headline"]["stopped"] == 2


def test_the_other_patients_blocked_entry_belongs_to_janes_report(report) -> None:
    assert any(t["seq"] == 6 for t in report["timeline"])
    kermit = incident.build_report(KERMIT, NIGHT, RECEIVED, INTACT)
    assert kermit["stopped"]["blocked"] == []   # the alert is filed under Jane


def test_intact_record(report) -> None:
    assert report["integrity"] == {"ok": True, "checked": 16, "broken_at": None, "problem": "",
                                   "head": "f" * 16, "in_report": False}
    assert report["period"]["since_seq"] == 1 and report["period"]["entries"] == 12
    assert [s["reason"] for s in report["system"]] == ["finale prepared",
                                                       "scenario.wrong_patient=off"]


def test_cover_up_is_exposed() -> None:
    """The insider rewrote the blocked call as an allowed read: the chain breaks there, the
    alert no longer matches the gateway's entry, and OPA's own log still says deny."""
    tampered = [dict(e) for e in NIGHT]
    tampered[5] = {**tampered[5], "decision": "allow", "severity": "info",
                   "reason": "get_coverage: 1 returned"}
    broken = {"ok": False, "checked": 5, "broken_at": 6,
              "problem": "entry #6 was modified after it was written"}
    report = incident.build_report(JANE, tampered, RECEIVED, broken)
    assert report["integrity"]["ok"] is False and report["integrity"]["in_report"]
    [blocked] = report["stopped"]["blocked"]
    assert not blocked["consistent"] and blocked["attempt"]["altered"]
    assert blocked["witness"]["decision"] == "deny"
    assert any(t["seq"] == 6 and t["altered"] for t in report["timeline"])


def test_deleted_attempt_is_reported_missing() -> None:
    gone = [e for e in NIGHT if e["seq"] != 6]
    [blocked] = incident.build_report(JANE, gone, RECEIVED, None, chain_problem="down")[
        "stopped"]["blocked"]
    assert blocked["missing"] and blocked["attempt"] is None and not blocked["consistent"]


def test_unverifiable_record() -> None:
    report = incident.build_report(JANE, NIGHT, [], None, chain_problem="audit service down")
    assert report["integrity"] == {"ok": None, "problem": "audit service down"}
    assert all(d["agrees"] is None for d in report["left"])   # no partner logs to compare


def test_empty_period() -> None:
    report = incident.build_report(JANE, [], [], INTACT)
    assert report["who"] == [] and report["period"]["entries"] == 0
    assert report["period"]["first_at"] is None


def test_period_start_and_patients() -> None:
    marks = [{"seq": 3, "action": "demo.reset"}, {"seq": 9, "action": "demo.finale_prepared"},
             {"seq": 12, "action": "auth.login"}]
    assert incident.period_start(marks)["seq"] == 9
    assert incident.period_start([]) is None
    assert incident.patients_in(NIGHT)[0] == {"id": JANE, "entries": 11}
    assert {"id": KERMIT, "entries": 1} in incident.patients_in(NIGHT)   # OPA not counted


def test_actor_of() -> None:
    assert incident.actor_of({"writer": "medrec-agent", "agent_chain": ["orchestrator",
                                                                       "medrec-agent"]}) \
        == "medrec-agent"
    assert incident.actor_of({"writer": "ehr-mcp", "agent_chain": ["summary-agent",
                                                                  "ehr-mcp"]}) == "summary-agent"
    assert incident.actor_of({"writer": "hospital-ui", "agent_chain": []}) == "hospital-ui"
