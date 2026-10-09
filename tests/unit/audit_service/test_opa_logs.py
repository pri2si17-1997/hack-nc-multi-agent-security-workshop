import gzip
import json

from hacknc.audit_service import opa_logs

DENY_LOG = {
    "decision_id": "d-1",
    "path": "hospital/approval/decision",
    "input": {"action": "decide", "subject": {"user": "nurse.gawain", "roles": ["nurse"],
                                              "client": "hospital-ui"},
              "resource": "approval/abc", "patient_id": "p1"},
    "result": {"allow": False, "reason": "only physicians may approve controlled substances"},
    "trace_id": "t" * 32,
}


def test_deny_decision_becomes_warning_event() -> None:
    event = opa_logs.to_event(DENY_LOG)
    assert event.action == "policy.decide"
    assert event.decision == "deny" and event.severity == "warning"
    assert event.actor_user == "nurse.gawain" and event.actor_role == "nurse"
    assert event.agent_chain == ["hospital-ui"]
    assert event.resource == "approval/abc"
    assert event.policy == "hospital/approval/decision"
    assert event.trace_id == "t" * 32
    assert event.details == {"opa_decision_id": "d-1"}


def test_non_hospital_policies_are_ignored() -> None:
    assert opa_logs.to_event({**DENY_LOG, "path": "system/health"}) is None
    assert opa_logs.to_event({**DENY_LOG, "result": True}) is None


def test_parse_gzip_body() -> None:
    body = gzip.compress(json.dumps([DENY_LOG]).encode())
    assert opa_logs.parse_body(body, "gzip") == [DENY_LOG]
    assert opa_logs.parse_body(json.dumps(DENY_LOG).encode(), None) == [DENY_LOG]
