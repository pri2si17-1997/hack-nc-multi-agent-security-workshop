"""POST /chat: the coordinator, with agents faked, in rules mode and with a scripted model."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from hacknc.common.auth import JwtValidator
from hacknc.common.auth.fastapi import BearerAuth
from hacknc.llm import LlmOutcome
from hacknc.orchestrator import app as orchestrator
from hacknc.orchestrator import coordinator
from hacknc.orchestrator.discharge import Discharge, DischargeBoard
from tests.conftest import ISSUER
from tests.fakes import RecordingAudit
from tests.unit.orchestrator.test_discharge import RESULTS, Flags, headers, statuses


class ScriptedModel:
    """Stands in for LlmRunner: calls the coordinator's tools in a fixed order, like a model
    would, and records which tools it was offered."""

    def __init__(self, script):
        self.script, self.offered, self.results = script, [], []

    def enabled(self):
        return True

    async def run(self, *, agent, task, system, prompt, tools=None, hooks=None, timeout=None):
        by_name = {t.tool_name: t for t in tools}
        self.offered.append(sorted(by_name))
        for name, kwargs in self.script:
            out = by_name[name](**kwargs)
            self.results.append(await out if hasattr(out, "__await__") else out)
        return LlmOutcome("done: " + " | ".join(r.splitlines()[0] for r in self.results),
                          "bedrock", "gpt-oss-20b", tools_used=[n for n, _ in self.script])


@pytest.fixture
def make_env(monkeypatch, key_provider):
    def build(llm=None):
        calls = []

        async def fake_call(deps, task, user_token, tool, arguments):
            calls.append((task.agent, tool, arguments))
            return RESULTS[tool]

        async def fake_record(deps, patient_id, user_token):
            return {"discharge": {"disposition": "home"}}

        monkeypatch.setattr(orchestrator, "_call", fake_call)
        monkeypatch.setattr(orchestrator, "_ehr_record_discharge", fake_record)
        deps = orchestrator.OrchestratorDeps(keycloak=None, secrets=lambda c: "s",
                                             agent_urls={}, audit=RecordingAudit(),
                                             flags=Flags(), admin_key="k", llm=llm)
        auth = BearerAuth(JwtValidator(ISSUER, "orchestrator", key_provider=key_provider))
        return TestClient(orchestrator.create_app(deps, auth, DischargeBoard())), calls, deps

    return build


def chat(http, make_token, message, scope=None):
    h = headers(make_token, scope) if scope else headers(make_token)
    return http.post("/chat", headers=h, json={"patient_id": "jane-doe", "message": message})


def test_rules_mode_discharge_from_chat(make_env, make_token) -> None:
    http, calls, deps = make_env()
    body = chat(http, make_token, "Discharge Jane Doe").json()
    assert body["by"].startswith("⚙ rules")
    assert statuses(body["discharge"])["prescriptions"] == "needs_signature"
    assert [c[1] for c in calls][0] == "reconcile_medications"
    assert "discharge.started" in deps.audit.actions()


def test_rules_mode_questions_and_status(make_env, make_token) -> None:
    http, calls, _ = make_env()
    assert chat(http, make_token, "What are her allergies?").json()["reply"] == "Penicillin"
    assert calls[0][1] == "answer_question"
    assert chat(http, make_token, "What's left?").json()["reply"] == "No discharge under way."


def test_nurse_is_refused_in_chat(make_env, make_token) -> None:
    http, calls, deps = make_env()
    body = chat(http, make_token, "Discharge Jane Doe", "agent:delegate patient:read").json()
    assert "may not order a discharge" in body["reply"] and "discharge:order" in body["reply"]
    assert calls == [] and body["discharge"]["status"] == "not_started"
    assert "discharge.refused" in deps.audit.actions()


def test_model_orchestrates_and_passes_findings_on(make_env, make_token) -> None:
    model = ScriptedModel([("start_discharge", {}), ("reconcile_medications", {}),
                           ("prepare_prescriptions", {"notes": "penicillin allergy"})])
    http, calls, _ = make_env(model)
    body = chat(http, make_token, "Discharge Jane Doe").json()
    assert body["by"] == "✨ Written by gpt-oss-20b"
    rx = next(c for c in calls if c[1] == "prepare_prescriptions")
    assert rx[2]["notes"] == "penicillin allergy"
    assert rx[2]["medication_plan"] == RESULTS["reconcile_medications"]["discharge_medications"]
    assert "record_discharge" not in model.offered[0]          # the readiness guard


def test_model_cannot_skip_prerequisites_or_start_without_permission(make_env,
                                                                     make_token) -> None:
    model = ScriptedModel([("prepare_prescriptions", {}), ("start_discharge", {}),
                           ("draft_discharge_summary", {})])
    http, calls, _ = make_env(model)
    chat(http, make_token, "Discharge Jane Doe", "agent:delegate patient:read")
    assert model.results[0].startswith("Refused: no discharge under way")
    assert model.results[1].startswith("Refused:") and "discharge:order" in model.results[1]
    assert calls == []


def test_prerequisites_are_enforced_in_code(make_env, make_token) -> None:
    model = ScriptedModel([("start_discharge", {}), ("draft_discharge_summary", {})])
    http, calls, _ = make_env(model)
    chat(http, make_token, "Discharge Jane Doe")
    assert "blocked" in model.results[1] and "medication reconciliation" in model.results[1]
    # What the model left is run afterwards, in the fixed order: reconciliation first.
    tools = [c[1] for c in calls]
    assert tools.index("reconcile_medications") < tools.index("draft_discharge_summary")


def test_rules_finish_what_the_model_left(make_env, make_token) -> None:
    """A model that stops early (say, after one agent is suspended) can't leave the discharge
    half done: every task it didn't run is run afterwards, and the reply says so."""
    model = ScriptedModel([("start_discharge", {}), ("reconcile_medications", {})])
    http, calls, _ = make_env(model)
    body = chat(http, make_token, "Discharge Jane Doe").json()
    assert statuses(body["discharge"])["prescriptions"] == "needs_signature"
    assert statuses(body["discharge"])["instructions"] == "done"
    assert "↪ rules ran what the model left" in body["reply"]
    assert [c[1] for c in calls].count("reconcile_medications") == 1     # not run twice


def test_a_question_runs_no_tasks(make_env, make_token) -> None:
    model = ScriptedModel([("answer_question", {"question": "Allergies?"})])
    http, calls, _ = make_env(model)
    chat(http, make_token, "What is she allergic to?")
    assert [c[1] for c in calls] == ["answer_question"]


def test_readiness_guard() -> None:
    d = Discharge("jane-doe")
    assert coordinator.ready_to_record(d)[0] is False
    for task in d.tasks:
        task.status = "done"
    d.task("prescriptions").signatures = [{"signature_id": "1", "status": "awaiting_signature"}]
    d.status = "completed"
    ok, why = coordinator.ready_to_record(d)
    assert not ok and "not signed" in why
    d.task("prescriptions").signatures[0]["status"] = "sent"
    assert coordinator.ready_to_record(d)[0] is True


def test_a_reasoning_scrap_before_the_bullets_is_dropped() -> None:
    assert coordinator.tidy_reply("Need to explain.- **Discharge started**\n- Next") == \
        "- **Discharge started**\n- Next"
    for kept in ("- **Discharge started**", "Penicillin (hives).",
                 "Jane is allergic to penicillin.\n- hives", "**Note:** - x"):
        assert coordinator.tidy_reply(kept) == kept
