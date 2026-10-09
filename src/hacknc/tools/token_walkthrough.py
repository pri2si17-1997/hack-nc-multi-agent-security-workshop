"""Token walkthrough: watch a user's token travel through the agent chain.

Run it (with the docker compose stack up):

    python -m hacknc.tools.token_walkthrough --user dr.morgana
    python -m hacknc.tools.token_walkthrough --user nurse.gawain
    python -m hacknc.tools.token_walkthrough --user bill.clerk

Part 1 - the happy path. The user logs in, then each hop exchanges the token it
received for a new one addressed to the next service:

    user -> orchestrator -> summary-agent -> ehr-mcp

At every hop the audience changes, the requesting agent (``azp``) changes, the
scopes stay narrow, and the human (``sub``) never disappears.

Part 2 - things that must NOT work, each blocked by Keycloak configuration alone.

The scenario itself lives in ``hacknc.demo.delegation``; this module only logs in
and prints tables. Every step is an OpenTelemetry span, so the whole walkthrough
appears as one trace in Grafana.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

import jwt
from opentelemetry import trace
from rich.console import Console
from rich.table import Table

from hacknc.common.auth import (
    JwtValidator,
    KeycloakClient,
    TokenClaims,
    TokenRequestError,
    peek_claims,
)
from hacknc.common.config import client_secret, get_settings
from hacknc.common.telemetry import format_trace_id, setup_tracing
from hacknc.demo.delegation import (
    DEMO_USERS,
    DENIED_CASES,
    HAPPY_PATH,
    HUMAN_SCOPES,
    LOGIN_CLIENT,
    DeniedCase,
    StepResult,
    run_chain,
    run_denied_case,
)

tracer = trace.get_tracer(__name__)


# --------------------------------------------------------------------------- rendering


def _claims_row(label: str, claims: TokenClaims | None, outcome: str) -> list[str]:
    if claims is None:
        return [label, "-", "-", "-", "-", "-", outcome]
    return [
        label,
        claims.username,
        claims.authorized_party,
        ", ".join(claims.audience),
        " ".join(sorted(claims.scopes)) or "(none)",
        ", ".join(sorted(claims.realm_roles)) or "(none)",
        outcome,
    ]


def _new_table(title: str) -> Table:
    table = Table(title=title, show_lines=True, title_justify="left")
    for column in ("Step", "Acting for (sub)", "Requested by (azp)", "Audience (aud)",
                   "Scopes", "Human roles", "Result"):
        table.add_column(column, overflow="fold")
    return table


def render_happy_path(console: Console, user_claims: TokenClaims, steps: list[StepResult]) -> None:
    table = _new_table("Part 1: delegation chain")
    table.add_row(*_claims_row("0. User logs in", user_claims, "[green]logged in[/]"))
    for i, step in enumerate(steps, start=1):
        label = f"{i}. {step.hop.requester} -> {step.hop.audience}"
        outcome = f"[green]✔ {step.reason}[/]" if step.granted else f"[red]✘ {step.reason}[/]"
        table.add_row(*_claims_row(label, step.claims, outcome))
    console.print(table)


def render_denied_cases(console: Console, cases: Sequence[tuple[DeniedCase, StepResult]]) -> None:
    table = Table(title="Part 2: things that must NOT work", show_lines=True, title_justify="left")
    for column in ("Attempt", "Expected", "Actual", "Why"):
        table.add_column(column, overflow="fold")
    for case, result in cases:
        actual = "[red]ALLOWED (unexpected!)[/]" if result.granted else "[green]DENIED[/]"
        table.add_row(case.title, "DENIED", actual, f"{case.lesson}\n[dim]{result.reason}[/]")
    console.print(table)


# --------------------------------------------------------------------------- entry point


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--user", choices=DEMO_USERS, default="dr.morgana")
    parser.add_argument("--show-tokens", action="store_true", help="print the raw JWTs too")
    args = parser.parse_args(argv)

    settings = get_settings()
    console = Console()
    if settings.demo_user_password is None:
        console.print("[red]DEMO_USER_PASSWORD is not set (copy .env.example to .env)[/]")
        return 2

    provider = setup_tracing("token-walkthrough", settings.otel_exporter_otlp_endpoint)
    kc = KeycloakClient(settings.keycloak_url)
    keys = jwt.PyJWKClient(kc.jwks_uri, cache_keys=True)

    def validators(audience: str) -> JwtValidator:
        return JwtValidator(settings.keycloak_issuer, audience, key_provider=keys)

    with tracer.start_as_current_span("token_walkthrough") as root:
        root.set_attribute("user.name", args.user)
        try:
            login = kc.password_login(
                LOGIN_CLIENT, args.user, settings.demo_user_password.get_secret_value(),
                HUMAN_SCOPES,
            )
        except TokenRequestError as exc:
            console.print(f"[red]Login failed for {args.user}: {exc}[/]")
            return 1
        user_claims = peek_claims(login.access_token)

        steps = run_chain(kc, client_secret, validators, login.access_token, HAPPY_PATH)
        denied = [
            (case, run_denied_case(kc, client_secret, validators, login.access_token, case))
            for case in DENIED_CASES
        ]
        trace_id = format_trace_id(root.get_span_context().trace_id)

    render_happy_path(console, user_claims, steps)
    render_denied_cases(console, denied)
    if args.show_tokens:
        console.print("\n[bold]User token[/]\n" + login.access_token)
        for step in steps:
            if step.token:
                hop = f"{step.hop.requester} -> {step.hop.audience}"
                console.print(f"\n[bold]{hop}[/]\n{step.token}")

    console.print(f"\nTrace id: [bold cyan]{trace_id}[/]")
    console.print("Find it in Grafana: http://localhost:3000 -> Explore -> Tempo -> paste the id")
    provider.shutdown()

    unexpected = [case.title for case, result in denied if result.granted]
    return 1 if unexpected else 0


if __name__ == "__main__":
    raise SystemExit(main())
