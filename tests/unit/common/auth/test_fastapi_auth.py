import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from hacknc.common.auth import JwtValidator, TokenClaims
from hacknc.common.auth.fastapi import BearerAuth
from tests.conftest import ISSUER


@pytest.fixture
def client(key_provider) -> TestClient:
    auth = BearerAuth(JwtValidator(ISSUER, "audit-service", key_provider=key_provider))
    app = FastAPI()

    @app.get("/who")
    def who(claims: TokenClaims = Depends(auth)) -> dict:
        return {"user": claims.username}

    return TestClient(app)


def test_valid_bearer_token(client, make_token) -> None:
    token = make_token(aud="audit-service")
    assert client.get("/who", headers={"Authorization": f"Bearer {token}"}).json() == {
        "user": "dr.morgana"}


@pytest.mark.parametrize("header", [None, "Basic abc", "Bearer ", "Bearer not-a-jwt"])
def test_missing_or_bad_token_is_401(client, header) -> None:
    headers = {"Authorization": header} if header else {}
    response = client.get("/who", headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")


def test_wrong_audience_is_401(client, make_token) -> None:
    token = make_token(aud="approval-service")
    response = client.get("/who", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert "not addressed to 'audit-service'" in response.json()["detail"]
