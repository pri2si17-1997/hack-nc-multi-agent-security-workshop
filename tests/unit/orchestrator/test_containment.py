"""A rogue agent is contained: alert from the gateway's audit entries, task suspended,
discharge on hold until a physician reviews. Agents, gateway and audit faked."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from hacknc.common.auth import JwtValidator
from hacknc.common.auth.fastapi import BearerAuth
from hacknc.orchestrator import app as orchestrator
from hacknc.orchestrator.discharge import DischargeBoard
from tests.conftest import ISSUER
from tests.fakes import RecordingAudit
from tests.unit.orchestrator.test_discharge import RESULTS, Flags, headers, statuses


class FakeWatch:
    """What the EHR gateway's audit entries say each agent was denied."""

    def __init__(self):
        self.rogue: set[str] = set()
        self.seq = 100

    async def denials(self, agent, after_seq=0):
        if agent not in self.rogue:
            return []
        self.seq += 1
        return [{"seq": self.seq, "action": "mcp.tool_call", "patient_id": "jane-doe",
                 "agent_chain": [agent, "ehr-mcp"], "details": {"tool": "get_clinical_notes"},
                 "reason": f"{agent} is not allowed to use get_clinical_notes"}]


class FakeAssigner:
    def __init__(self):
        self.calls = []

    def assign(self, agent, user, patient_id):
        self.calls.append((agent, user, patient_id))

    def release(self, agent, user, patient_id):
        pass


@pytest.fixture
def env(monkeypatch, key_provider):
    async def fake_call_tool_path(deps, task, user_token, tool, arguments):
        if deps.assigner is not None and arguments.get("patient_id"):
            deps.assigner.assign(task.agent, "dr.morgana", arguments["patient_id"])
        return RESULTS[tool]

    async def fake_record(deps, patient_id, user_token):
        return {"discharge": {"disposition": "home"}}

    monkeypatch.setattr(orchestrator, "_call", fake_call_tool_path)
    monkeypatch.setattr(orchestrator, "_ehr_record_discharge", fake_record)
    watch, assigner = FakeWatch(), FakeAssigner()
    deps = orchestrator.OrchestratorDeps(keycloak=None, secrets=lambda c: "s", agent_urls={},
                                         audit=RecordingAudit(), flags=Flags(), admin_key="k",
                                         watch=watch, assigner=assigner)
    auth = BearerAuth(JwtValidator(ISSUER, "orchestrator", key_provider=key_provider))
    return TestClient(orchestrator.create_app(deps, auth, DischargeBoard())), watch, deps


def test_rogue_agent_is_suspended_and_the_discharge_held(env, make_token) -> None:
    http, watch, deps = env
    watch.rogue = {"scheduling-agent"}
    view = http.post("/discharges/jane-doe/start", headers=headers(make_token)).json()
    assert view["status"] == "on_hold"
    assert statuses(view)["scheduling"] == "suspended"
    assert statuses(view)["instructions"] == "blocked"             # needed the booking
    [alert] = view["alerts"]
    assert alert["agent"] == "scheduling-agent" and alert["status"] == "open"
    scheduling = next(t for t in view["tasks"] if t["id"] == "scheduling")
    assert scheduling["data"] == {}                                # its result is not used
    alert_events = [e for e in deps.audit.events if e.action == "security.alert"]
    assert alert_events[0].severity == "high"

    # Signing everything does not finish it: the hold stays until a physician reviews.
    for sid in ("rx1", "sum1"):
        view = http.post("/discharges/jane-doe/signed", headers=headers(make_token),
                         json={"signature_id": sid}).json()
    assert view["status"] == "on_hold" and not (view["ehr_record"] or {}).get("recorded")


def test_only_a_physician_can_acknowledge_and_the_task_is_retried(env, make_token) -> None:
    http, watch, deps = env
    watch.rogue = {"scheduling-agent"}
    view = http.post("/discharges/jane-doe/start", headers=headers(make_token)).json()
    alert_id = view["alerts"][0]["id"]
    url = f"/discharges/jane-doe/alerts/{alert_id}/acknowledge"
    nurse = http.post(url, headers=headers(make_token, "agent:delegate patient:read"))
    assert nurse.status_code == 403

    watch.rogue = set()                       # the agent was fixed (admin switched it off)
    view = http.post(url, headers=headers(make_token)).json()
    assert view["alerts"][0]["status"] == "acknowledged"
    assert statuses(view)["scheduling"] == "done" and statuses(view)["instructions"] == "done"
    assert view["status"] == "awaiting_signatures"
    [ack] = [e for e in deps.audit.events if e.action == "security.alert_acknowledged"]
    assert ack.details["alert_id"] == alert_id                # the report links review to alert
    assert ack.details["gateway_audit_seq"] == view["alerts"][0]["seq"]
    assert http.post(url, headers=headers(make_token)).status_code == 409   # once


def test_still_rogue_after_review_is_suspended_again(env, make_token) -> None:
    http, watch, _ = env
    watch.rogue = {"scheduling-agent"}
    view = http.post("/discharges/jane-doe/start", headers=headers(make_token)).json()
    view = http.post(f"/discharges/jane-doe/alerts/{view['alerts'][0]['id']}/acknowledge",
                     headers=headers(make_token)).json()
    assert view["status"] == "on_hold" and len(view["alerts"]) == 2
    assert statuses(view)["scheduling"] == "suspended"


def test_every_task_is_bound_to_its_patient(env, make_token) -> None:
    http, _, deps = env
    http.post("/discharges/jane-doe/start", headers=headers(make_token))
    agents = {a for a, _, p in deps.assigner.calls if p == "jane-doe"}
    assert agents >= {"medrec-agent", "benefits-agent", "scheduling-agent",
                      "prescription-agent", "summary-agent", "instructions-agent"}


def test_call_assigns_the_patient_before_the_agent_gets_a_token(monkeypatch, make_token) -> None:
    import anyio

    from hacknc.agents.runtime import AgentError
    from hacknc.orchestrator.discharge import Task

    order = []

    class Assigner:
        def assign(self, agent, user, patient_id):
            order.append(("assign", agent, user, patient_id))

        def release(self, agent, user, patient_id):
            order.append(("release", agent, user, patient_id))

    async def fake_delegate(keycloak, secrets, requester, token, audience, scopes):
        order.append(("delegate", audience))
        raise AgentError("stop here")

    monkeypatch.setattr(orchestrator, "delegate", fake_delegate)
    deps = orchestrator.OrchestratorDeps(keycloak=None, secrets=lambda c: "s",
                                         agent_urls={}, assigner=Assigner())
    task = Task("benefits", "Benefits", "benefits-agent", ("patient:read",))
    user_token = make_token(preferred_username="dr.morgana")
    anyio.run(orchestrator._call, deps, task, user_token, "arrange_equipment",
              {"patient_id": "jane-doe"})
    assert order == [("assign", "benefits-agent", "dr.morgana", "jane-doe"),
                     ("delegate", "benefits-agent"),
                     ("release", "benefits-agent", "dr.morgana", "jane-doe")]   # even on failure
    assert task.status == "failed"


def test_still_rogue_after_everything_was_signed_stays_on_hold(env, make_token) -> None:
    """The runbook's order: sign every draft while on hold, then review with the agent still
    rogue. The review must not complete the discharge before the retry has run."""
    http, watch, _ = env
    watch.rogue = {"scheduling-agent"}
    view = http.post("/discharges/jane-doe/start", headers=headers(make_token)).json()
    for sid in ("rx1", "sum1"):
        view = http.post("/discharges/jane-doe/signed", headers=headers(make_token),
                         json={"signature_id": sid}).json()
    view = http.post(f"/discharges/jane-doe/alerts/{view['alerts'][0]['id']}/acknowledge",
                     headers=headers(make_token)).json()
    assert view["status"] == "on_hold" and statuses(view)["scheduling"] == "suspended"
    assert not (view["ehr_record"] or {}).get("recorded")

    watch.rogue = set()                                    # fixed: the next review finishes it
    view = http.post(f"/discharges/jane-doe/alerts/{view['alerts'][1]['id']}/acknowledge",
                     headers=headers(make_token)).json()
    assert view["status"] == "completed" and view["ehr_record"]["recorded"]


def test_an_agent_that_does_not_answer_fails_its_task_not_the_request(monkeypatch,
                                                                        make_token) -> None:
    """A timeout reaching an agent (not a refusal) must not leave the task 'running' forever."""
    import anyio

    from hacknc.agents.runtime import Delegation
    from hacknc.orchestrator.discharge import Discharge

    async def fake_delegate(*args, **kwargs):
        return Delegation("t", None)

    def timing_out(*args, **kwargs):
        raise TimeoutError("read timed out")

    monkeypatch.setattr(orchestrator, "delegate", fake_delegate)
    monkeypatch.setattr(orchestrator, "mcp_session", timing_out)
    deps = orchestrator.OrchestratorDeps(keycloak=None, secrets=lambda c: "s",
                                         agent_urls={"medrec-agent": "http://x"})
    task = Discharge("jane-doe").task("medrec")
    result = anyio.run(orchestrator._call, deps, task, "tok", "reconcile_medications", {})
    assert result is None and task.status == "failed"
    assert "did not answer" in task.detail and "read timed out" in task.detail
