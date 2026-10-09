from types import SimpleNamespace

from hacknc.agents import compose
from hacknc.agents.servers import _rx_check
from hacknc.agents.toolkit import DecisionLog, ToolLog, VerifyCalls


def before(name, args, selected=True):
    return SimpleNamespace(tool_use={"name": name, "input": args},
                           selected_tool=object() if selected else None, cancel_tool=False)


def after(name, args, result, exception=None):
    return SimpleNamespace(tool_use={"name": name, "input": args}, selected_tool=object(),
                           result=result, exception=exception)


def test_record_decision_returns_structured_data() -> None:
    log = DecisionLog()
    assert not log.made
    assert log.tool(decision={"specialty": "cardiology"}, rationale="NSTEMI",
                    evidence=["gen-obs-trop"]) == "Decision recorded."
    assert log.decision == {"specialty": "cardiology"} and log.evidence == ["gen-obs-trop"]


def test_tool_log_keeps_gateway_answers_and_refusals() -> None:
    log = ToolLog()
    log.after(after("book_follow_up", {"specialty": "cardiology"},
                    {"status": "success", "structuredContent": {"response": {"x": 1}}}))
    log.after(after("get_clinical_notes", {}, {"status": "error", "content": [
        {"text": "DENIED by policy: scheduling-agent may not read notes"}]}))
    log.after(after("record_decision", {}, {"status": "success"}))
    assert [c.tool for c in log.results("book_follow_up")] == ["book_follow_up"]
    assert log.refused == ["get_clinical_notes: DENIED by policy: scheduling-agent may not "
                           "read notes"]


def test_unknown_tools_are_recorded_as_blocked() -> None:
    log = ToolLog()
    log.before(before("send_email", {"to": "x"}, selected=False))
    assert log.blocked == ["tried send_email: not in its toolbox"]


def test_verify_calls_cancels_before_the_gateway() -> None:
    log = ToolLog()
    hook = VerifyCalls(lambda tool, args: "too many" if args.get("n", 0) > 1 else None, log)
    ok, bad = before("t", {"n": 1}), before("t", {"n": 5})
    hook.before(ok)
    hook.before(bad)
    assert ok.cancel_tool is False and "too many" in bad.cancel_tool
    assert log.blocked == [bad.cancel_tool]


def test_prescription_drafts_must_match_the_verified_plan() -> None:
    plan = [{"prescription": {"drug": "doxycycline", "dose": "100 mg", "quantity": 10,
                              "sig": "bid"}}]
    check = _rx_check("jane-doe", plan)
    exact = {"patient_id": "jane-doe", "drug": "doxycycline", "dose": "100 mg",
             "quantity": 10, "sig": "bid"}
    titled = {"title": "Doxycycline 100 mg × 10"}
    assert check("request_signature", {"kind": "prescription", "content": exact,
                                       **titled}) is None
    # "10" would be fingerprinted and sent as text: not the plan's prescription.
    assert "exactly" in check("request_signature", {
        "kind": "prescription", "content": {**exact, "quantity": "10"}})
    assert "does not match" in check("request_signature", {
        "kind": "prescription", "content": {**exact, "quantity": 300}})
    assert check("request_signature", {"kind": "discharge_summary", "content": {}})
    assert check("get_medications", {}) is None
    # Once is enough: a second identical draft would be a second prescription.
    assert "already been drafted" in check("request_signature",
                                           {"kind": "prescription", "content": exact,
                                            **titled})


def test_drafted_title_and_schedule_must_describe_the_content() -> None:
    """The card shows title and schedule: the physician must not sign oxycodone under a
    title that says doxycycline, or without its Schedule II chip."""
    oxy = {"drug": "oxycodone", "dose": "10 mg", "quantity": 20, "sig": "q6h prn"}
    check = _rx_check("jane-doe", [{"prescription": oxy, "controlled": "C-II"}])
    content = {"patient_id": "jane-doe", **oxy}
    draft = {"kind": "prescription", "content": content, "title": "Oxycodone 10 mg × 20",
             "schedule": "C-II"}
    assert "title must be exactly" in check("request_signature",
                                            {**draft, "title": "Doxycycline 100 mg"})
    assert "schedule must be C-II" in check("request_signature", {**draft, "schedule": None})
    assert check("request_signature", draft) is None


def test_model_proposal_is_verified_by_rules() -> None:
    verified = [{"name": "Doxycycline 100 mg", "action": "new"},
                {"name": "Oxycodone hydrochloride 5 MG", "action": "renew"},
                {"name": "24 HR Metformin hydrochloride 500 MG", "action": "resume"},
                {"name": "Home oxygen", "action": "new", "dme": True}]
    proposed = [{"drug": "doxycycline", "action": "new"},
                {"drug": "metformin", "action": "stop", "why": "kidneys"},
                {"drug": "amoxicillin-clavulanate", "action": "new"}]
    titles = [f["title"] for f in compose.compare_proposal(proposed, verified)]
    assert titles == ["Model and rules disagree on 24 HR Metformin hydrochloride 500 MG",
                      "Model proposed amoxicillin-clavulanate: not in the verified plan",
                      "Model left out Oxycodone hydrochloride 5 MG"]
