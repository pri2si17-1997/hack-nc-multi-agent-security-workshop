"""The coordinator's checklist logic, with agents faked (no network)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from hacknc.common.auth import JwtValidator
from hacknc.common.auth.fastapi import BearerAuth
from hacknc.orchestrator import app as orchestrator
from hacknc.orchestrator.discharge import DischargeBoard
from tests.conftest import ISSUER
from tests.fakes import RecordingAudit

RESULTS = {
    "reconcile_medications": {"text": "2 issues", "discrepancies": [{}, {}],
                              "discharge_medications": [{"name": "Doxycycline", "action": "new"}]},
    "arrange_equipment": {"text": "ok", "authorization": {"status": "approved",
                                                            "authorization": "PA-1"}},
    "book_follow_up": {"text": "ok", "appointment": {"clinician": "Dr. Lee", "when": "Oct 9"}},
    "prepare_prescriptions": {"text": "ok", "prescriptions": [
        {"signature_id": "rx1", "title": "Doxycycline", "content": {"drug": "doxycycline"}}]},
    "draft_discharge_summary": {"text": "ok", "signature_id": "sum1", "summary": {"s": 1}},
    "write_patient_instructions": {"text": "ok", "injection_warnings": []},
    "send_prescription": {"text": "sent", "sent": True},
    "send_summary_to_pcp": {"text": "filed", "sent": True},
    "answer_question": {"text": "Penicillin"},
}


class Flags:
    def __init__(self, off=()):
        self.off = set(off)

    def on(self, key):
        return key not in self.off


@pytest.fixture
def env(monkeypatch, key_provider):
    calls = []

    async def fake_call(deps, task, user_token, tool, arguments):
        calls.append((task.agent, tool))
        return RESULTS[tool]

    async def fake_record(deps, patient_id, user_token):
        calls.append(("orchestrator", "record_discharge"))
        return {"status": "discharged", "discharge": {"disposition": "home",
                                                      "ordered_by": "dr.morgana",
                                                      "at": "2026-10-03T14:05:00"}}

    monkeypatch.setattr(orchestrator, "_call", fake_call)
    monkeypatch.setattr(orchestrator, "_ehr_record_discharge", fake_record)
    deps = orchestrator.OrchestratorDeps(keycloak=None, secrets=lambda c: "s", agent_urls={},
                                         audit=RecordingAudit(), flags=Flags(),
                                         admin_key="k")
    auth = BearerAuth(JwtValidator(ISSUER, "orchestrator", key_provider=key_provider))
    return TestClient(orchestrator.create_app(deps, auth, DischargeBoard())), calls, deps


def headers(make_token, scope="agent:delegate patient:read discharge:order rx:approve"):
    return {"Authorization": "Bearer " + make_token(aud="orchestrator", azp="hospital-ui",
                                                     scope=scope)}


def statuses(view):
    return {t["id"]: t["status"] for t in view["tasks"]}


def test_nurse_cannot_order_a_discharge(env, make_token) -> None:
    http, calls, _ = env
    response = http.post("/discharges/jane-doe/start",
                         headers=headers(make_token, "agent:delegate patient:read"))
    assert response.status_code == 403
    assert "discharge:order" in response.json()["detail"]
    assert calls == []


def test_start_runs_every_agent_and_waits_for_signatures(env, make_token) -> None:
    http, calls, deps = env
    view = http.post("/discharges/jane-doe/start", headers=headers(make_token)).json()
    assert view["status"] == "awaiting_signatures"
    assert statuses(view) == {
        "medrec": "done", "benefits": "done", "scheduling": "done",
        "prescriptions": "needs_signature", "summary": "needs_signature", "instructions": "done",
        "send_prescriptions": "waiting", "send_summary": "waiting"}
    assert [c[1] for c in calls][:3] == ["reconcile_medications", "arrange_equipment",
                                        "book_follow_up"]
    assert deps.audit.actions() == ["discharge.started"] + ["coordinator.step"] * 6


def test_signing_sends_and_completes(env, make_token) -> None:
    http, calls, _ = env
    h = headers(make_token)
    http.post("/discharges/jane-doe/start", headers=h)
    http.post("/discharges/jane-doe/signed", json={"signature_id": "rx1"}, headers=h)
    view = http.post("/discharges/jane-doe/signed", json={"signature_id": "sum1"},
                     headers=h).json()
    assert ("prescription-agent", "send_prescription") in calls
    assert ("transitions-agent", "send_summary_to_pcp") in calls
    assert view["status"] == "completed"
    assert statuses(view)["send_prescriptions"] == "sent"
    assert calls.count(("orchestrator", "record_discharge")) == 1
    assert view["ehr_record"]["recorded"] and view["ehr_record"]["ordered_by"] == "dr.morgana"


def test_a_completed_discharge_cannot_run_again(env, make_token) -> None:
    http, calls, _ = env
    h = headers(make_token)
    http.post("/discharges/jane-doe/start", headers=h)
    for sid in ("rx1", "sum1"):
        http.post("/discharges/jane-doe/signed", json={"signature_id": sid}, headers=h)
    before = len(calls)
    again = http.post("/discharges/jane-doe/start", headers=h)
    assert again.status_code == 409 and "complete" in again.json()["detail"]
    assert http.post("/discharges/jane-doe/reopen", json={}, headers=h).status_code == 409
    assert len(calls) == before          # no agent ran, nothing re-sent


def test_a_discharge_under_way_must_be_reopened(env, make_token) -> None:
    http, calls, deps = env
    h = headers(make_token)
    http.post("/discharges/jane-doe/start", headers=h)
    again = http.post("/discharges/jane-doe/start", headers=h)
    assert again.status_code == 409 and "reopen" in again.json()["detail"]

    nurse = headers(make_token, "agent:delegate patient:read")
    assert http.post("/discharges/jane-doe/reopen", json={}, headers=nurse).status_code == 403
    view = http.post("/discharges/jane-doe/reopen", json={"reason": "plan changed"},
                     headers=h).json()
    assert view["status"] == "not_started"
    assert sorted(view["cancelled_signatures"]) == ["rx1", "sum1"]
    assert deps.audit.actions()[-1] == "discharge.reopened"
    assert http.post("/discharges/jane-doe/start", headers=h).status_code == 200


def test_rejecting_sends_nothing(env, make_token) -> None:
    http, calls, _ = env
    h = headers(make_token)
    http.post("/discharges/jane-doe/start", headers=h)
    view = http.post("/discharges/jane-doe/signed",
                     json={"signature_id": "rx1", "decision": "rejected"}, headers=h).json()
    assert ("prescription-agent", "send_prescription") not in calls
    assert statuses(view)["prescriptions"] == "done"


def test_rejecting_the_last_draft_lets_the_discharge_finish(env, make_token) -> None:
    """Regression: rejecting the last open one: the send task used to stay
    'waiting for the rest of your signatures' and the discharge never completed."""
    http, calls, _ = env
    h = headers(make_token)
    http.post("/discharges/jane-doe/start", headers=h)
    http.post("/discharges/jane-doe/signed", json={"signature_id": "sum1"}, headers=h)
    view = http.post("/discharges/jane-doe/signed",
                     json={"signature_id": "rx1", "decision": "rejected"}, headers=h).json()
    send = next(t for t in view["tasks"] if t["id"] == "send_prescriptions")
    assert send["status"] == "sent" and send["summary"].startswith("Nothing sent")
    assert view["status"] == "completed"


def test_switched_off_agent_blocks_its_dependents(env, make_token) -> None:
    http, calls, deps = env
    deps.flags = Flags(off={"agent.medrec-agent"})
    view = http.post("/discharges/jane-doe/start", headers=headers(make_token)).json()
    s = statuses(view)
    assert s["medrec"] == "disabled"
    assert s["prescriptions"] == s["summary"] == "blocked"
    assert s["benefits"] == "done"


def test_chat_goes_to_the_chart_agent(env, make_token) -> None:
    http, calls, _ = env
    answer = http.post("/chat", json={"patient_id": "jane-doe", "message": "allergies?"},
                       headers=headers(make_token)).json()
    assert answer["reply"] == "Penicillin" and calls == [("chart-agent", "answer_question")]


def test_admin_reset_needs_the_key(env, make_token) -> None:
    http, *_ = env
    assert http.delete("/admin/discharges").status_code == 401
    assert http.delete("/admin/discharges", headers={"X-Demo-Admin-Key": "k"}).status_code == 204


def test_an_unsigned_draft_keeps_the_discharge_open(env, make_token) -> None:
    """Regression: with the summary's sender switched off, signing the prescriptions must not
    complete the discharge (and record it in the EHR) while the summary is unsigned."""
    http, calls, deps = env
    deps.flags = Flags(off={"agent.transitions-agent"})
    h = headers(make_token)
    http.post("/discharges/jane-doe/start", headers=h)
    view = http.post("/discharges/jane-doe/signed", json={"signature_id": "rx1"},
                     headers=h).json()
    assert statuses(view)["send_summary"] == "disabled"
    assert view["status"] == "awaiting_signatures"
    assert ("orchestrator", "record_discharge") not in calls
    view = http.post("/discharges/jane-doe/signed", json={"signature_id": "sum1"},
                     headers=h).json()
    assert view["status"] == "completed" and view["ehr_record"]["recorded"]
