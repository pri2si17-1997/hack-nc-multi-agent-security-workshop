"""Guardrails around the model."""

from __future__ import annotations

from types import SimpleNamespace

from hacknc.llm.guards import GatewayFacts, PinPatient, check_output


def tool_call(name, **args):
    return SimpleNamespace(tool_use={"name": name, "toolUseId": "t1", "input": args},
                           cancel_tool=False)


def test_tool_calls_about_this_patient_pass() -> None:
    pin = PinPatient("jane-doe")
    event = tool_call("get_allergies", patient_id="jane-doe")
    pin.check(event)
    assert event.cancel_tool is False and pin.cancelled == []


def test_tool_calls_about_another_patient_are_cancelled() -> None:
    pin = PinPatient("jane-doe")
    event = tool_call("get_clinical_notes", patient_id="pt-87c46349")
    pin.check(event)
    assert "about patient jane-doe only" in event.cancel_tool
    assert pin.cancelled == [event.cancel_tool]


def test_tool_calls_without_a_patient_are_cancelled() -> None:
    event = tool_call("search_patients", query="smith")
    PinPatient("jane-doe").check(event)
    assert "no patient" in event.cancel_tool


def test_output_checks() -> None:
    assert check_output("- Take doxycycline twice a day.") == []
    assert check_output("Send it to records-request@fastmail.example") == [
        "contains an email address"]
    assert "contains a link" in check_output("See https://example.com")
    assert "talks about its own instructions" in check_output("I will ignore previous rules")
    assert check_output("  ") == ["is empty"]


def after(name, result):
    return SimpleNamespace(tool_use={"name": name, "input": {}}, result=result)


def test_gateway_facts_come_from_tool_results_not_the_model() -> None:
    facts = GatewayFacts()
    notes = {"status": "success", "structuredContent": {
        "notes": [], "withheld": [{"type": "DocumentReference",
                                   "reason": "PSY-labelled record"}]}}
    facts.collect(after("get_clinical_notes", notes))
    facts.collect(after("get_clinical_notes", notes))          # asked twice: counted once
    denied = {"status": "error", "content": [
        {"text": "DENIED by policy: chart-agent is not allowed to use get_coverage"}]}
    facts.collect(after("get_coverage", denied))
    facts.collect(after("get_coverage", denied))
    facts.collect(after("get_allergies", {"status": "success",
                                          "structuredContent": {"withheld": []}}))
    assert facts.withheld == [{"type": "DocumentReference", "reason": "PSY-labelled record"}]
    assert facts.refused == ["get_coverage: DENIED by policy: chart-agent is not allowed to "
                             "use get_coverage"]


def test_gateway_facts_never_raise() -> None:
    facts = GatewayFacts()
    facts.collect(after("x", {"status": "success", "structuredContent": {"withheld": "oops"}}))
    facts.collect(SimpleNamespace(tool_use=None, result=None))


def test_pin_patient_lets_the_agents_own_tools_through() -> None:
    from types import SimpleNamespace

    from hacknc.llm.guards import PinPatient

    hook = PinPatient("jane-doe", frozenset({"record_decision"}))
    local = SimpleNamespace(tool_use={"name": "record_decision", "input": {}}, cancel_tool=False)
    other = SimpleNamespace(tool_use={"name": "get_medications",
                                      "input": {"patient_id": "pt-1"}}, cancel_tool=False)
    hook.check(local)
    hook.check(other)
    assert local.cancel_tool is False and "pt-1" in other.cancel_tool


def test_malformed_tool_input_is_cancelled_not_a_crash() -> None:
    event = SimpleNamespace(tool_use={"name": "get_allergies", "input": "jane-doe"},
                            cancel_tool=False)
    PinPatient("jane-doe").check(event)
    assert "no patient" in event.cancel_tool
