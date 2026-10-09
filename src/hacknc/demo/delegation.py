"""The delegation scenario: which agent hands work to which, and what must be refused.

Used by the token-walkthrough CLI and the UI's delegation page. Every hop is a real
RFC 8693 token exchange against Keycloak, recorded as an OpenTelemetry span.

    user -> orchestrator -> summary-agent -> ehr-mcp

Phase 4 replaces this simulation with the real agents doing their own exchanges;
the scenario definitions here stay useful as the expected behaviour.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from opentelemetry import trace

from hacknc.common.auth import (
    JwtValidator,
    KeycloakClient,
    TokenClaims,
    TokenRequestError,
    TokenValidationError,
)

LOGIN_CLIENT = "workshop-cli"
HUMAN_SCOPES = ("agent:delegate", "patient:read", "discharge:order", "rx:approve",
                "portal:self")
DEMO_USERS = ("dr.morgana", "nurse.gawain", "admin.percival")

tracer = trace.get_tracer(__name__)

SecretLookup = Callable[[str], str]
ValidatorFactory = Callable[[str], JwtValidator]


@dataclass(frozen=True)
class Hop:
    """One delegation step: ``requester`` exchanges its token for one addressed to ``audience``."""

    requester: str
    audience: str
    scopes: tuple[str, ...]
    description: str = ""


@dataclass(frozen=True)
class StepResult:
    """Outcome of one hop."""

    hop: Hop
    granted: bool
    reason: str
    claims: TokenClaims | None = None
    token: str | None = None


@dataclass(frozen=True)
class DeniedCase:
    """A request that must be refused, and the lesson it teaches."""

    title: str
    lesson: str
    attempt: Hop
    setup: tuple[Hop, ...] = ()
    use_user_token_directly: bool = False


HAPPY_PATH: tuple[Hop, ...] = (
    Hop("orchestrator", "summary-agent", ("patient:read",), "Orchestrator delegates the summary"),
    Hop("summary-agent", "ehr-mcp", ("patient:read",), "Summary agent calls the EHR gateway"),
)

DENIED_CASES: tuple[DeniedCase, ...] = (
    DeniedCase(
        title="Scheduling agent asks to file prescriptions",
        lesson="scheduling-agent was never given sign:request",
        setup=(Hop("orchestrator", "scheduling-agent", ("patient:read",)),),
        attempt=Hop("scheduling-agent", "ehr-mcp", ("sign:request",)),
    ),
    DeniedCase(
        title="Summary agent sends data outside",
        lesson="summary-agent may only talk to ehr-mcp, never the disclosure gateway",
        setup=(Hop("orchestrator", "summary-agent", ("patient:read",)),),
        attempt=Hop("summary-agent", "disclosure-mcp", ("disclose:care-transition",)),
    ),
    DeniedCase(
        title="Coordinator tries to sign a prescription",
        lesson="no agent holds rx:approve; only physicians sign",
        attempt=Hop("orchestrator", "prescription-agent", ("rx:approve",)),
    ),
    DeniedCase(
        title="Benefits agent replays the user's token",
        lesson="a token can only be exchanged by a service in its audience (no passthrough)",
        attempt=Hop("benefits-agent", "disclosure-mcp", ("disclose:prior-auth",)),
        use_user_token_directly=True,
    ),
)


def perform_hop(
    kc: KeycloakClient,
    secrets: SecretLookup,
    validators: ValidatorFactory,
    subject_token: str,
    hop: Hop,
) -> StepResult:
    """Exchange ``subject_token`` as described by ``hop`` and check the result.

    A hop counts as granted only if Keycloak issued a token, the token validates
    for the target audience, and every requested scope is actually present.
    """
    with tracer.start_as_current_span(f"token_exchange {hop.requester} -> {hop.audience}") as span:
        span.set_attribute("agent.id", hop.requester)
        span.set_attribute("token.requested_audience", hop.audience)
        span.set_attribute("token.requested_scopes", " ".join(hop.scopes))
        try:
            tokens = kc.exchange_token(
                hop.requester, secrets(hop.requester), subject_token, hop.audience, hop.scopes
            )
        except TokenRequestError as exc:
            return _denied(span, hop, f"Keycloak refused: {exc.error} ({exc.description})")

        try:
            claims = validators(hop.audience).validate(tokens.access_token)
        except TokenValidationError as exc:
            return _denied(span, hop, f"token rejected by {hop.audience}: {exc}")

        missing = set(hop.scopes) - claims.scopes
        if missing:
            result = StepResult(hop, False, f"scope not granted: {', '.join(sorted(missing))}",
                                claims, tokens.access_token)
            _record(span, result)
            return result

        result = StepResult(hop, True, f"valid for {hop.audience}", claims, tokens.access_token)
        _record(span, result)
        return result


def run_chain(
    kc: KeycloakClient,
    secrets: SecretLookup,
    validators: ValidatorFactory,
    start_token: str,
    hops: Sequence[Hop],
) -> list[StepResult]:
    """Run hops in order, feeding each new token into the next hop. Stops at the first denial."""
    results: list[StepResult] = []
    token = start_token
    for hop in hops:
        result = perform_hop(kc, secrets, validators, token, hop)
        results.append(result)
        if not result.granted or result.token is None:
            break
        token = result.token
    return results


def run_denied_case(
    kc: KeycloakClient,
    secrets: SecretLookup,
    validators: ValidatorFactory,
    user_token: str,
    case: DeniedCase,
) -> StepResult:
    """Prepare the starting token (via ``case.setup``) and then try the forbidden hop."""
    with tracer.start_as_current_span(f"denied_case {case.title}"):
        token = user_token
        if not case.use_user_token_directly and case.setup:
            setup = run_chain(kc, secrets, validators, user_token, case.setup)
            if not setup[-1].granted or setup[-1].token is None:
                return StepResult(case.attempt, False, f"setup step denied: {setup[-1].reason}")
            token = setup[-1].token
        return perform_hop(kc, secrets, validators, token, case.attempt)


def _denied(span: trace.Span, hop: Hop, reason: str) -> StepResult:
    result = StepResult(hop, False, reason)
    _record(span, result)
    return result


def _record(span: trace.Span, result: StepResult) -> None:
    span.set_attribute("token.granted", result.granted)
    span.set_attribute("token.reason", result.reason)
    if result.claims:
        span.set_attribute("user.name", result.claims.username)
        span.set_attribute("token.azp", result.claims.authorized_party)
        span.set_attribute("token.aud", ",".join(result.claims.audience))
        span.set_attribute("token.scope", " ".join(sorted(result.claims.scopes)))
