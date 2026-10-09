from __future__ import annotations

import gzip
import json

import pytest
from fastapi.testclient import TestClient

from hacknc.audit_service.app import create_app
from hacknc.audit_service.store import AuditStore
from hacknc.common.auth import JwtValidator
from hacknc.common.auth.fastapi import BearerAuth
from tests.conftest import ISSUER


@pytest.fixture
def store() -> AuditStore:
    return AuditStore(":memory:")


@pytest.fixture
def client(store, key_provider) -> TestClient:
    auth = BearerAuth(JwtValidator(ISSUER, "audit-service", key_provider=key_provider))
    return TestClient(create_app(store, auth, opa_log_token="opa-secret"))


@pytest.fixture
def writer(make_token):
    def _token(client_id="hospital-ui", scope="audit:write"):
        return {"Authorization": "Bearer " + make_token(aud="audit-service", azp=client_id,
                                                         scope=scope)}
    return _token


def test_requires_a_token(client) -> None:
    assert client.get("/events").status_code == 401
    assert client.post("/events", json={"action": "x"}).status_code == 401


def test_token_for_another_service_is_rejected(client, make_token) -> None:
    headers = {"Authorization": "Bearer " + make_token(aud="approval-service",
                                                        scope="audit:write")}
    assert client.post("/events", json={"action": "x"}, headers=headers).status_code == 401


def test_append_uses_token_client_as_writer(client, writer, store) -> None:
    response = client.post("/events", headers=writer("approval-service"),
                           json={"action": "approval.approve", "decision": "allow"})
    assert response.status_code == 201
    assert store.recent()[0]["writer"] == "approval-service"


def test_write_requires_audit_write_scope(client, writer) -> None:
    response = client.post("/events", headers=writer(scope="audit:read"), json={"action": "x"})
    assert response.status_code == 403


def test_read_and_verify(client, writer) -> None:
    for decision in ("allow", "deny"):
        client.post("/events", headers=writer(), json={"action": "x", "decision": decision})
    reader = writer(scope="audit:read")
    body = client.get("/events", headers=reader, params={"decision": "deny"}).json()
    assert [e["decision"] for e in body["entries"]] == ["deny"]
    assert body["total"] == 2
    assert client.get("/verify", headers=reader).json()["ok"] is True


def test_opa_logs_need_the_shared_token(client, writer, store) -> None:
    log = [{"path": "hospital/approval/decision", "input": {"action": "view", "subject": {}},
            "result": {"allow": True, "reason": "allowed"}}]
    body = gzip.compress(json.dumps(log).encode())
    headers = {"Authorization": "Bearer opa-secret", "Content-Encoding": "gzip"}
    assert client.post("/opa/logs", content=body, headers=headers).status_code == 204
    assert store.recent()[0]["writer"] == "opa"

    # A service's normal Keycloak token is not enough to pose as OPA.
    other = {**writer("hospital-ui"), "Content-Encoding": "gzip"}
    assert client.post("/opa/logs", content=body, headers=other).status_code == 401


def test_read_a_period(client, writer) -> None:
    for _ in range(4):
        client.post("/events", headers=writer(), json={"action": "x"})
    reader = writer(scope="audit:read")
    body = client.get("/events", headers=reader, params={"after": 1, "before": 4}).json()
    assert [e["seq"] for e in body["entries"]] == [3, 2]
    assert client.get("/events", headers=reader, params={"after": -1}).status_code == 422
