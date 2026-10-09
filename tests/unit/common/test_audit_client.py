import json

import httpx

from hacknc.common.audit import AuditClient, AuditEvent, actor_fields
from hacknc.common.auth import TokenClaims


class StaticTokens:
    def token(self):
        return "svc-token"


def client_with(handler) -> AuditClient:
    return AuditClient("http://audit.test", StaticTokens(),
                       http=httpx.Client(transport=httpx.MockTransport(handler)))


def test_record_posts_event_with_service_token() -> None:
    seen = {}

    def handler(request):
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"seq": 1})

    assert client_with(handler).record(AuditEvent(action="auth.login", decision="allow"))
    assert seen["auth"] == "Bearer svc-token"
    assert seen["body"]["action"] == "auth.login"


def test_failure_is_reported_not_raised() -> None:
    assert client_with(lambda r: httpx.Response(500)).record(AuditEvent(action="x")) is False


def test_actor_fields_pick_hospital_role() -> None:
    claims = TokenClaims({"preferred_username": "nurse.gawain",
                          "realm_access": {"roles": ["offline_access", "nurse"]}})
    assert actor_fields(claims) == {"actor_user": "nurse.gawain", "actor_role": "nurse"}
    assert actor_fields(None) == {"actor_user": None, "actor_role": None}


def test_actor_role_covers_every_hospital_role() -> None:
    from hacknc.common.audit import actor_fields
    from hacknc.common.auth import TokenClaims

    for role in ("physician", "nurse", "admin", "patient"):
        claims = TokenClaims({"preferred_username": "u", "realm_access": {"roles": [role]}})
        assert actor_fields(claims)["actor_role"] == role
