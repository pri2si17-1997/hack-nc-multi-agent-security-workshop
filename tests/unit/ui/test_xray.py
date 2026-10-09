import re
from pathlib import Path

from hacknc.ui.otlp import SpanRecord
from hacknc.ui.xray import STORY_AUDIT_ACTIONS, steps_for


def span(name, service="svc", events=(), **attrs) -> SpanRecord:
    return SpanRecord("t1", "s1", "", name, service, 10, 20, attrs, list(events))


def test_clinician_actions_are_privilege_steps_from_the_user() -> None:
    [reopen] = steps_for(span("ui.action", **{"user.name": "dr.morgana", "ui.action": "reopen"}))
    assert reopen.node == "user" and reopen.pillar == "privilege"
    assert reopen.title == "dr.morgana reopens the discharge"
    [chat] = steps_for(span("ui.action", **{"user.name": "dr.morgana", "ui.action": "chat",
                                            "question": "Allergies?"}))
    assert "Allergies?" in chat.title


def test_granted_and_refused_token_exchanges() -> None:
    granted = steps_for(span("token_exchange orchestrator -> summary-agent", **{
        "token.requester": "orchestrator", "token.requested_audience": "summary-agent",
        "token.granted": True, "token.scope": "patient:read", "user.name": "nurse.gawain"}))[0]
    assert granted.status == "ok" and granted.node == "keycloak"
    assert "Summary agent" in granted.title and "patient:read" in granted.caption

    refused = steps_for(span("token_exchange orchestrator -> billing-agent", **{
        "token.requester": "orchestrator", "token.requested_audience": "billing-agent",
        "token.granted": False, "token.reason": "billing:read not granted"}))[0]
    assert refused.status == "deny" and "billing:read" in refused.caption


def test_ehr_tool_allowed_with_withheld_records_is_a_warning() -> None:
    [step] = steps_for(span("ehr-mcp get_clinical_notes", **{
        "mcp.tool": "get_clinical_notes", "agent.id": "summary-agent",
        "policy.decision": "allow", "ehr.returned": 2, "ehr.withheld": 1,
        "ehr.withheld_reasons": "PSY-labelled record"}))
    assert step.status == "warn" and "1 withheld" in step.caption


def test_ehr_tool_denied_by_opa() -> None:
    [step] = steps_for(span("ehr-mcp get_clinical_notes", **{
        "mcp.tool": "get_clinical_notes", "agent.id": "billing-agent",
        "policy.decision": "deny", "policy.reason": "billing-agent is not allowed"}))
    assert step.status == "deny" and step.node == "opa"


def test_agent_reasoning_findings_and_injection_become_steps() -> None:
    steps = steps_for(span("agent medrec-agent: compare medication lists",
                           events=[{"name": "agent.finding",
                                    "attributes": {"severity": "high", "detail": "Allergy"}},
                                   {"name": "security.injection_suspected",
                                    "attributes": {"detail": "portal message"}}],
                           **{"agent.id": "medrec-agent"}))
    assert [s.status for s in steps] == ["info", "warn", "warn"]
    assert steps[1].title == "Finding: Allergy"


def test_scenario_events_on_the_agent_span() -> None:
    steps = steps_for(span("agent prescription-agent: send_prescription",
                           events=[{"name": "agent.misbehaving",
                                    "attributes": {"approved": "5 mg x20",
                                                   "submitted": "5 mg x300"}}],
                           **{"agent.id": "prescription-agent", "agent.task": "send"}))
    assert "alters the signed prescription" in steps[1].title
    assert steps[1].t > steps[0].t
    rogue = steps_for(span("agent scheduling-agent: book_follow_up",
                           events=[{"name": "agent.misbehaving",
                                    "attributes": {"tried": "get_clinical_notes"}}],
                           **{"agent.id": "scheduling-agent", "agent.task": "book"}))
    assert "goes rogue" in rogue[1].title


def test_disclosures_and_partner_receipts() -> None:
    [sent] = steps_for(span("disclosure-mcp prior_authorization", **{
        "agent.id": "benefits-agent", "disclosure.purpose": "prior_authorization",
        "disclosure.destination": "payer", "policy.decision": "allow",
        "disclosure.sent_fields": "name,dob,evidence", "disclosure.stripped": 2,
        "disclosure.stripped_reasons": "PSY-labelled record"}))
    assert sent.status == "warn" and sent.node == "disclosure-mcp" and "payer" in sent.also
    assert "Withheld 2" in sent.caption
    [blocked] = steps_for(span("disclosure-mcp eligibility", **{
        "agent.id": "benefits-agent", "disclosure.purpose": "eligibility",
        "disclosure.destination": "payer", "policy.decision": "deny",
        "policy.reason": "payer is switched off"}))
    assert blocked.status == "deny"
    [received] = steps_for(span("POST /payer/prior-auth", service="partners",
                                **{"partner.name": "payer", "partner.fields": "name,dob"}))
    assert received.node == "payer"


def test_approval_and_audit_steps() -> None:
    [created] = steps_for(span("policy create", service="approval-service",
                               **{"policy.decision": "allow"}))
    assert created.node == "approvals" and "signature queue" in created.title
    [refused] = steps_for(span("policy consume", service="approval-service", **{
        "policy.decision": "deny", "policy.reason": "differs from what the physician approved"}))
    assert refused.status == "deny"
    [audit] = steps_for(span("POST /events", **{"audit.seq": 42,
                                                 "audit.action": "disclosure.sent",
                                                 "audit.decision": "allow",
                                                 "audit.writer": "disclosure-mcp",
                                                 "audit.hash": "ab"}))
    assert audit.title == "Audit #42: disclosure.sent" and audit.pillar == "accountability"
    # Routine audit entries (every tool call) stay off the panel to keep the story readable.
    assert steps_for(span("POST /events", **{"audit.seq": 43, "audit.action": "mcp.tool_call",
                                             "audit.decision": "allow"})) == []


def test_plumbing_spans_are_ignored() -> None:
    assert steps_for(span("POST /mcp")) == []
    assert steps_for(span("policy view", service="approval-service")) == []


def test_model_call_with_tools_and_a_fallback() -> None:
    steps = steps_for(span("llm chart-agent: answer question", events=[
        {"name": "llm.fallback", "attributes": {"provider": "bedrock",
                                                "reason": "ExpiredToken"}}], **{
        "agent.id": "chart-agent", "llm.task": "answer question", "llm.used_model": True,
        "llm.label": "Qwen on Ollama (local)", "llm.tools_used": "get_allergies",
        "gen_ai.usage.input_tokens": 900, "gen_ai.usage.output_tokens": 80}))
    assert [s.icon for s in steps] == ["✨", "↪️"]
    assert "get_allergies" in steps[0].caption and "OPA" in steps[0].caption
    assert steps[1].status == "warn" and "ExpiredToken" in steps[1].caption


def test_no_model_means_a_rules_step() -> None:
    [step] = steps_for(span("llm summary-agent: write hospital course", **{
        "agent.id": "summary-agent", "llm.task": "write hospital course",
        "llm.used_model": False, "llm.skipped": "AI model switched off by the admin"}))
    assert step.icon == "⚙" and "switched off" in step.caption


def test_discharge_recorded_and_reopened_are_story_audit_steps() -> None:
    for action in ("encounter.discharged", "discharge.reopened"):
        [step] = steps_for(span("audit append", **{"audit.seq": 7, "audit.action": action,
                                                   "audit.decision": "allow",
                                                   "audit.writer": "ehr-mcp"}))
        assert action in step.title


def test_steps_name_their_actor_and_map_edge() -> None:
    [denied] = steps_for(span("ehr-mcp get_clinical_notes", **{
        "mcp.tool": "get_clinical_notes", "agent.id": "scheduling-agent",
        "policy.decision": "deny", "policy.reason": "not this agent's job"}))
    data = denied.to_dict()
    assert data["actor"] == "scheduling-agent"
    assert (data["from"], data["to"], data["status"]) == ("scheduling-agent", "ehr-mcp", "deny")
    [chat] = steps_for(span("ui.action", **{"user.name": "dr.morgana", "ui.action": "chat",
                                            "question": "Discharge Jane Doe"}))
    assert chat.to_dict()["to"] == "orchestrator" and "Discharge Jane Doe" in chat.title


def test_model_calls_light_the_model_node() -> None:
    [step] = steps_for(span("llm benefits-agent: request prior authorization", **{
        "agent.id": "benefits-agent", "llm.task": "request prior authorization",
        "llm.used_model": True, "llm.label": "gpt-oss-20b",
        "llm.tools_used": "get_patient_summary,request_prior_authorization",
        "gen_ai.usage.input_tokens": 1200, "gen_ai.usage.output_tokens": 300}))
    assert step.node == "model" and step.to_dict()["from"] == "benefits-agent"
    assert step.details["tokens in / out"] == "1200 / 300"


def test_decisions_and_blocked_attempts_become_steps() -> None:
    steps = steps_for(span("agent medrec-agent: reconcile_medications", events=[
        {"name": "agent.decision", "attributes": {"decision": "9 items, 2 findings",
                                                  "rationale": "penicillin allergy",
                                                  "by": "✨ gpt-oss-20b"}},
        {"name": "agent.blocked", "attributes": {"detail": "tried send_email: not in its "
                                                           "toolbox"}}],
        **{"agent.id": "medrec-agent", "agent.task": "reconcile_medications"}))
    decided, blocked = steps[1], steps[2]
    assert decided.title == "Med-rec agent decided: 9 items, 2 findings"
    assert decided.pillar == "accountability" and "penicillin" in decided.caption
    assert blocked.status == "deny" and "not in its toolbox" in blocked.caption


def test_security_alert_is_a_red_accountability_step() -> None:
    [step] = steps_for(span("orchestrator security alert", **{
        "alert.agent": "scheduling-agent", "alert.tool": "get_clinical_notes",
        "alert.reason": "scheduling-agent is not allowed to use get_clinical_notes",
        "alert.audit_seq": 364}))
    data = step.to_dict()
    assert step.title == "Security alert: Scheduling agent suspended"
    assert (data["from"], data["to"], data["status"]) == ("scheduling-agent", "orchestrator",
                                                          "deny")
    assert "#364" in step.caption


def test_collector_tags_the_same_audit_milestones_as_story_spans() -> None:
    """Grafana's story=true filter (Collector) must match the steps the activity map shows."""
    config = (Path(__file__).parents[3] / "infra/observability/otel-collector.yaml").read_text()
    [alternation] = re.findall(r'audit\.action"\], "\^\((.*?)\)\$"', config)
    assert set(alternation.replace("\\\\.", ".").split("|")) == STORY_AUDIT_ACTIONS
