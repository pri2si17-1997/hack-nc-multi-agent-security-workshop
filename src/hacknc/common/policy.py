"""Ask Open Policy Agent (OPA) for a decision.

Services describe *who* wants to do *what* to *which resource* as a JSON ``input``;
OPA evaluates the Rego policy and answers ``{"allow": ..., "reason": ...}``.

The service enforces; OPA decides. If OPA cannot be reached the answer is **deny**
(fail closed): in healthcare, "the policy engine is down" must never mean "allow everything".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from hacknc.common.auth.models import TokenClaims


@dataclass(frozen=True)
class PolicyDecision:
    allow: bool
    reason: str
    policy: str
    decision_id: str | None = None


class OpaClient:
    """Thin client for OPA's data API (``POST /v1/data/<path>``)."""

    def __init__(self, base_url: str, http: httpx.Client | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._http = http or httpx.Client(timeout=5.0)

    def query(self, policy: str, input_doc: dict[str, Any]) -> Any | None:
        """Evaluate a rule that returns data (a list, a string...). ``None`` if OPA is
        unreachable or the rule is undefined; callers must treat that as the safe answer."""
        try:
            response = self._http.post(f"{self.base_url}/v1/data/{policy}",
                                       json={"input": input_doc})
            response.raise_for_status()
            return response.json().get("result")
        except (httpx.HTTPError, ValueError):
            return None

    def decide(self, policy: str, input_doc: dict[str, Any]) -> PolicyDecision:
        """Evaluate ``policy`` (e.g. ``hospital/approval/decision``) against ``input_doc``.

        The rule must return an object with ``allow`` (bool) and ``reason`` (str).
        """
        try:
            response = self._http.post(f"{self.base_url}/v1/data/{policy}",
                                       json={"input": input_doc})
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            return PolicyDecision(False, f"policy engine unavailable ({exc.__class__.__name__})",
                                  policy)
        result = body.get("result")
        if not isinstance(result, dict) or "allow" not in result:
            return PolicyDecision(False, "policy returned no decision", policy,
                                  body.get("decision_id"))
        return PolicyDecision(bool(result["allow"]), str(result.get("reason", "")), policy,
                              body.get("decision_id"))


def subject_input(claims: TokenClaims) -> dict[str, Any]:
    """The caller, as policies see it: the human, their roles, and the app/agent acting."""
    return {
        "user": claims.username,
        "sub": claims.subject,
        "roles": sorted(claims.realm_roles),
        "client": claims.authorized_party,
        "scopes": sorted(claims.scopes),
    }
