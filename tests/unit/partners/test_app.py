from datetime import date

import pytest
from fastapi.testclient import TestClient

from hacknc.common.auth import JwtValidator
from hacknc.common.fingerprint import fingerprint
from hacknc.partners.app import create_app
from tests.conftest import ISSUER


@pytest.fixture
def client(key_provider) -> TestClient:
    def validators(partner):
        return JwtValidator(ISSUER, partner, key_provider=key_provider)

    return TestClient(create_app(validators, "admin-key", today=lambda: date(2026, 10, 3)))


def auth(make_token, partner):
    return {"Authorization": "Bearer " + make_token(aud=partner, azp="disclosure-mcp")}


def test_partner_only_accepts_its_own_audience(client, make_token) -> None:
    response = client.post("/payer/eligibility", json={"member_id": "m"},
                           headers=auth(make_token, "retail-pharmacy"))
    assert response.status_code == 401
    assert "Blue Cross Blue Shield rejected the token" in response.json()["detail"]


def test_prior_auth_needs_a_qualifying_saturation(client, make_token) -> None:
    headers = auth(make_token, "payer")
    approved = client.post("/payer/prior-auth", headers=headers, json={
        "service": "Home oxygen", "evidence": ["Oxygen saturation, walking: 86 % (2026-10-02)"]})
    assert approved.json()["status"] == "approved"
    pended = client.post("/payer/prior-auth", headers=headers,
                         json={"service": "Home oxygen", "evidence": []})
    assert pended.json()["status"] == "pended"


def test_pharmacy_rechecks_the_signature(client, make_token) -> None:
    headers = auth(make_token, "retail-pharmacy")
    rx = {"drug": "oxycodone", "quantity": 20}
    ok = client.post("/retail-pharmacy/prescriptions", headers=headers, json={
        "prescription": rx, "signature": {"fingerprint": fingerprint(rx)}})
    assert ok.json()["status"] == "received"
    forged = client.post("/retail-pharmacy/prescriptions", headers=headers, json={
        "prescription": {**rx, "quantity": 300}, "signature": {"fingerprint": fingerprint(rx)}})
    assert forged.status_code == 422


def test_pcp_books_within_the_window_and_logs_receipts(client, make_token) -> None:
    booked = client.post("/pcp-clinic/appointments", headers=auth(make_token, "pcp-clinic"),
                         json={"name": "Jane Doe", "within_days": 7}).json()
    assert booked["when"].startswith("2026-10-09")
    received = client.get("/admin/received", headers={"X-Demo-Admin-Key": "admin-key"}).json()
    assert received["received"][0]["fields"] == ["name", "within_days"]
    assert received["received"][0]["sent_by"] == "disclosure-mcp"
    assert client.delete("/admin/received", headers={"X-Demo-Admin-Key": "admin-key"}
                         ).status_code == 204
    assert client.get("/admin/received", headers={"X-Demo-Admin-Key": "x"}).status_code == 401


def test_registry_returns_fill_history(client, make_token) -> None:
    body = client.post("/nc-csrs/query", headers=auth(make_token, "nc-csrs"),
                       json={"name": "Jane Doe", "drug": "oxycodone"}).json()
    assert body["prescribers"] == 1 and body["risk_flags"] == []


def test_follow_up_goes_to_the_requested_specialty(client, make_token) -> None:
    headers = auth(make_token, "pcp-clinic")
    cardio = client.post("/pcp-clinic/appointments", headers=headers,
                         json={"name": "Jeff Heathcote", "within_days": 7,
                               "specialty": "cardiology"}).json()
    assert cardio["clinician"] == "Dr. Maya Chen" and cardio["specialty"] == "cardiology"
    unknown = client.post("/pcp-clinic/appointments", headers=headers,
                          json={"name": "x", "specialty": "dermatology"})
    assert unknown.status_code == 422


def test_registry_has_no_history_for_most_patients(client, make_token) -> None:
    body = client.post("/nc-csrs/query", headers=auth(make_token, "nc-csrs"),
                       json={"name": "Tabatha Harber", "drug": "oxycodone"}).json()
    assert body["fills_last_12_months"] == 0 and body["last_fill"] is None
