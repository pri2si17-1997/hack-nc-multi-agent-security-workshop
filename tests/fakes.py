"""Fakes shared by unit tests. No network, no Docker."""

from __future__ import annotations

from hacknc.common.auth import TokenRequestError, TokenSet, peek_claims


class FakeKeycloak:
    """Grants exchanges according to a rule table: (requester, audience) -> granted scopes.

    Like real Keycloak, it also refuses an exchange when the requester is not in the
    subject token's audience, and refuses pairs not in the table (unknown audience).
    """

    def __init__(self, make_token, rules: dict[tuple[str, str], set[str]], wrong_aud=False):
        self.make_token = make_token
        self.rules = rules
        self.wrong_aud = wrong_aud
        self.calls: list[tuple[str, str, str]] = []

    def exchange_token(self, client_id, client_secret, subject_token, audience, scopes=()):
        self.calls.append((client_id, audience, subject_token))
        if client_id not in peek_claims(subject_token).audience:
            raise TokenRequestError(403, "access_denied", "client is not within the token audience")
        if (client_id, audience) not in self.rules:
            raise TokenRequestError(400, "invalid_request", "audience not available")
        granted = self.rules[(client_id, audience)] & set(scopes)
        token = self.make_token(
            aud="someone-else" if self.wrong_aud else audience,
            azp=client_id,
            scope=" ".join(sorted(granted)),
        )
        return TokenSet(access_token=token)


ALLOWED = {
    ("orchestrator", "summary-agent"): {"patient:read"},
    ("orchestrator", "scheduling-agent"): {"patient:read"},
    ("orchestrator", "prescription-agent"): {"patient:read", "sign:request"},
    ("summary-agent", "ehr-mcp"): {"patient:read", "sign:request"},
    ("scheduling-agent", "ehr-mcp"): {"patient:read"},
    ("benefits-agent", "disclosure-mcp"): {"disclose:prior-auth"},
}


class FakeOpa:
    """Stands in for OPA. ``rules`` maps action -> (allow, reason); default is allow.

    Records every input so tests can check what the service asked.
    """

    def __init__(self, rules=None):
        self.rules = rules or {}
        self.inputs: list[dict] = []

    def decide(self, policy, input_doc):
        from hacknc.common.policy import PolicyDecision

        self.inputs.append(input_doc)
        rule = self.rules.get(input_doc.get("action"), (True, "allowed"))
        allow, reason = rule(input_doc) if callable(rule) else rule
        return PolicyDecision(allow, reason, policy, "decision-1")


class RecordingAudit:
    """Stands in for AuditClient: keeps events in a list."""

    def __init__(self):
        self.events = []

    def record(self, event):
        self.events.append(event)
        return True

    def actions(self):
        return [e.action for e in self.events]
