import httpx

from hacknc.common.auth import TokenClaims
from hacknc.common.policy import OpaClient, subject_input


def opa_with(handler) -> OpaClient:
    return OpaClient("http://opa.test", http=httpx.Client(transport=httpx.MockTransport(handler)))


def test_decision_is_read_from_result() -> None:
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = request.content
        return httpx.Response(200, json={"decision_id": "d1",
                                         "result": {"allow": False, "reason": "nope"}})

    decision = opa_with(handler).decide("hospital/approval/decision", {"action": "decide"})
    assert seen["url"] == "http://opa.test/v1/data/hospital/approval/decision"
    assert b'"input"' in seen["body"]
    assert (decision.allow, decision.reason, decision.decision_id) == (False, "nope", "d1")


def test_opa_down_means_deny() -> None:
    def handler(request):
        raise httpx.ConnectError("refused")

    decision = opa_with(handler).decide("p", {})
    assert not decision.allow
    assert "unavailable" in decision.reason


def test_undefined_policy_means_deny() -> None:
    decision = opa_with(lambda r: httpx.Response(200, json={})).decide("p", {})
    assert not decision.allow
    assert decision.reason == "policy returned no decision"


def test_subject_input_from_claims() -> None:
    claims = TokenClaims({"preferred_username": "dr.morgana", "sub": "s", "azp": "hospital-ui",
                          "scope": "b a", "realm_access": {"roles": ["physician"]}})
    assert subject_input(claims) == {"user": "dr.morgana", "sub": "s", "roles": ["physician"],
                                     "client": "hospital-ui", "scopes": ["a", "b"]}
