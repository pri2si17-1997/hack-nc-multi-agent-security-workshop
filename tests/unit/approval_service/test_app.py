"""Approval service behaviour, with OPA and the audit log faked (policy logic has its own
Rego tests). Covers the full human-in-the-loop flow, including tamper detection."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from hacknc.approval_service.app import ApprovalSettings, create_app
from hacknc.approval_service.store import ApprovalStore
from hacknc.common.auth import JwtValidator
from hacknc.common.auth.fastapi import BearerAuth
from tests.conftest import ISSUER
from tests.fakes import FakeOpa, RecordingAudit

RX = {"patient_id": "p1", "drug": "oxycodone", "dose": "5 mg", "quantity": 20}
NEW_REQUEST = {"kind": "prescription", "patient_id": "p1", "patient_name": "Jane Doe",
               "title": "Oxycodone 5 mg x 20", "content": RX, "schedule": "C-II",
               "reason": "renew", "agent_chain": ["prescription-agent", "ehr-mcp"],
               "evidence": {"sources": ["MedicationRequest/1"], "warnings": []}}


FOR_P1 = {"patient_id": "p1", "kind": "prescription"}   # what the signature was given for


def consume_rule(input_doc):
    """Mirror of the Rego consume rule, enough to drive the service."""
    ok = (input_doc["request"]["status"] == "approved" and input_doc.get("hash_matches") is True
          and input_doc.get("submitted") == {"patient_id": input_doc["patient_id"],
                                             "kind": input_doc["kind"]})
    return ok, "allowed" if ok else "submitted prescription differs or not approved"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def env(key_provider, make_token):
    opa = FakeOpa({"consume": consume_rule})
    audit = RecordingAudit()
    clock = Clock()
    settings = ApprovalSettings(approval_ttl_seconds=300, demo_admin_key="admin-key")
    auth = BearerAuth(JwtValidator(ISSUER, "approval-service", key_provider=key_provider))
    app = create_app(ApprovalStore(":memory:"), auth, opa, audit, settings, clock)

    def headers(client_id="ehr-mcp", user="dr.morgana", roles=("physician",), scope=""):
        """Token for a caller (default: the EHR gateway acting for Dr. Pendragon)."""
        token = make_token(aud="approval-service", azp=client_id, preferred_username=user,
                           realm_access={"roles": list(roles)}, scope=scope)
        return {"Authorization": f"Bearer {token}"}

    return TestClient(app), opa, audit, clock, settings, headers


def file_request(client, headers) -> dict:
    response = client.post("/requests", json=NEW_REQUEST, headers=headers())
    assert response.status_code == 201, response.text
    return response.json()


def test_create_records_who_and_fingerprint(env) -> None:
    client, opa, audit, *_ , headers = env
    request = file_request(client, headers)
    assert request["status"] == "pending"
    assert request["requested_for"] == "dr.morgana"
    assert request["requested_by"] == "ehr-mcp"
    assert len(request["fingerprint"]) == 64
    assert opa.inputs[0]["action"] == "create"
    assert opa.inputs[0]["subject"]["client"] == "ehr-mcp"
    assert audit.actions() == ["approval.request"]


def test_policy_denial_is_403_and_audited(env) -> None:
    client, opa, audit, *_ , headers = env
    opa.rules["create"] = (False, "only the EHR gateway may file signature requests")
    response = client.post("/requests", json=NEW_REQUEST, headers=headers("prescription-agent"))
    assert response.status_code == 403
    assert "EHR gateway" in response.json()["reason"]
    assert audit.events[-1].decision == "deny"


def test_full_flow_approve_then_dispense(env) -> None:
    client, opa, audit, clock, _, headers = env
    request = file_request(client, headers)
    human = headers("hospital-ui", scope="rx:approve")

    approved = client.post(f"/requests/{request['id']}/approve", headers=human).json()
    assert approved["status"] == "approved"
    assert approved["decided_by"] == "dr.morgana"
    assert opa.inputs[-1]["action"] == "decide"
    assert opa.inputs[-1]["request"] == {"status": "pending", "expired": False}

    dispensed = client.post(f"/requests/{request['id']}/consume", json={"content": RX, **FOR_P1},
                            headers=headers("disclosure-mcp"))
    assert dispensed.status_code == 200
    assert dispensed.json()["status"] == "fulfilled"
    assert audit.actions() == ["approval.request", "approval.approve", "rx.fulfilled"]


def test_tampered_prescription_burns_the_approval(env) -> None:
    client, opa, audit, _, _, headers = env
    request = file_request(client, headers)
    client.post(f"/requests/{request['id']}/approve", headers=headers("hospital-ui"))

    swapped = {**RX, "quantity": 300}
    response = client.post(f"/requests/{request['id']}/consume",
                           json={"content": swapped, **FOR_P1},
                           headers=headers("disclosure-mcp"))
    assert response.status_code == 403
    assert opa.inputs[-1]["hash_matches"] is False
    tamper = audit.events[-1]
    assert tamper.action == "approval.tamper_detected" and tamper.severity == "high"
    assert tamper.details["submitted"]["quantity"] == 300

    # Even the original prescription can no longer use this approval.
    retry = client.post(f"/requests/{request['id']}/consume", json={"content": RX, **FOR_P1},
                        headers=headers("disclosure-mcp"))
    assert retry.status_code == 403
    stored = client.app.state.store.get(request["id"])
    assert stored.status == "invalidated" and stored.tampered_submission["quantity"] == 300


def test_signature_for_one_patient_cannot_send_for_another(env) -> None:
    client, opa, audit, _, _, headers = env
    request = file_request(client, headers)
    client.post(f"/requests/{request['id']}/approve", headers=headers("hospital-ui"))
    response = client.post(f"/requests/{request['id']}/consume",
                           json={"content": RX, "patient_id": "p2", "kind": "prescription"},
                           headers=headers("disclosure-mcp"))
    assert response.status_code == 403
    assert opa.inputs[-1]["submitted"] == {"patient_id": "p2", "kind": "prescription"}
    assert opa.inputs[-1]["patient_id"] == "p1"
    assert audit.events[-1].action == "approval.tamper_detected"
    assert client.app.state.store.get(request["id"]).status == "invalidated"   # burned


def test_approval_cannot_be_used_twice(env) -> None:
    client, _, _, _, _, headers = env
    request = file_request(client, headers)
    client.post(f"/requests/{request['id']}/approve", headers=headers("hospital-ui"))
    for expected in (200, 403):
        response = client.post(f"/requests/{request['id']}/consume", json={"content": RX, **FOR_P1},
                               headers=headers("disclosure-mcp"))
        assert response.status_code == expected


def test_expiry_is_passed_to_policy(env) -> None:
    client, opa, _, clock, _, headers = env
    request = file_request(client, headers)
    clock.now += 301
    client.post(f"/requests/{request['id']}/approve", headers=headers("hospital-ui"))
    assert opa.inputs[-1]["request"] == {"status": "expired", "expired": True}


def test_unknown_request_is_404(env) -> None:
    client, *_, headers = env
    assert client.post("/requests/nope/approve", headers=headers()).status_code == 404


def test_break_glass_grant_is_high_severity(env) -> None:
    client, opa, audit, _, settings, headers = env
    settings.flags["break_glass_enabled"] = True
    nurse = headers("hospital-ui", user="nurse.gawain", roles=("nurse",))
    response = client.post("/break-glass", headers=nurse,
                           json={"patient_id": "p1", "justification": "unconscious in ER"})
    assert response.status_code == 201
    assert opa.inputs[-1]["break_glass"] == {"enabled": True,
                                             "justification": "unconscious in ER"}
    assert audit.events[-1].severity == "high"
    grants = client.get("/break-glass/grants", params={"patient_id": "p1"}, headers=nurse).json()
    assert grants["grants"][0]["user"] == "nurse.gawain"

    # Switching break-the-glass off revokes active grants.
    client.put("/admin/flags", json={"break_glass_enabled": False},
               headers={"X-Demo-Admin-Key": "admin-key"})
    assert client.get("/break-glass/grants", headers=nurse).json()["grants"] == []


def test_admin_flags_need_the_key_and_merge(env) -> None:
    client, *_ = env
    assert client.get("/admin/flags").status_code == 401
    assert client.put("/admin/flags", json={"break_glass_enabled": True},
                      headers={"X-Demo-Admin-Key": "wrong"}).status_code == 401
    admin = {"X-Demo-Admin-Key": "admin-key"}
    flags = client.put("/admin/flags", json={"scenario.oversharing": True, "nonsense": True},
                       headers=admin).json()
    assert flags["scenario.oversharing"] is True
    assert flags["agent.medrec-agent"] is True
    assert "nonsense" not in flags


def test_admin_reset_empties_the_queue(env) -> None:
    client, *_, headers = env
    file_request(client, headers)
    assert client.delete("/admin/requests", headers={"X-Demo-Admin-Key": "admin-key"}
                         ).status_code == 204
    assert client.app.state.store.recent() == []


def test_simultaneous_submissions_use_a_signature_once(env) -> None:
    """Two submissions of the same signed prescription at the same moment: one is sent."""
    import threading
    import time as clock_time

    client, opa, _, _, _, headers = env
    request = file_request(client, headers)
    client.post(f"/requests/{request['id']}/approve", headers=headers("hospital-ui"))

    def slow_consume(input_doc):          # widen the window between check and save
        clock_time.sleep(0.05)
        return consume_rule(input_doc)

    opa.rules["consume"] = slow_consume
    codes: list[int] = []
    auth = headers("disclosure-mcp")

    def submit() -> None:
        with TestClient(client.app) as own:
            codes.append(own.post(f"/requests/{request['id']}/consume",
                                  json={"content": RX, **FOR_P1}, headers=auth).status_code)

    threads = [threading.Thread(target=submit) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(codes) == [200, 403, 403, 403]
