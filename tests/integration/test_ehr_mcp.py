"""ehr-mcp over real HTTP with real Keycloak tokens. Needs `make up`.

Tokens are obtained exactly like the agents will: user login -> orchestrator -> <agent> ->
ehr-mcp, each hop a token exchange. Run with: make test-integration
"""

from __future__ import annotations

import httpx
import httpx2
import jwt
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from hacknc.common.auth import JwtValidator, KeycloakClient
from hacknc.common.auth.service_tokens import ServiceTokenProvider
from hacknc.common.config import client_secret, get_settings
from hacknc.demo.delegation import HUMAN_SCOPES, LOGIN_CLIENT, Hop, run_chain
from hacknc.orchestrator.watch import TaskAssigner

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

MCP_URL = "http://localhost:8010/mcp"
AGENT_SCOPES = {"summary-agent": ("patient:read",), "scheduling-agent": ("patient:read",),
                "chart-agent": ("patient:read",), "portal-agent": ("portal:self",),
                "prescription-agent": ("patient:read", "sign:request")}


@pytest.fixture(scope="module")
def token_for():
    try:
        httpx.get("http://localhost:8010/healthz", timeout=3).raise_for_status()
    except httpx.HTTPError:
        pytest.skip("ehr-mcp is not reachable; run `make up` first")
    settings = get_settings()
    kc = KeycloakClient(settings.keycloak_url)
    keys = jwt.PyJWKClient(kc.jwks_uri)

    def validators(aud):
        return JwtValidator(settings.keycloak_issuer, aud, key_provider=keys)

    # Like the coordinator: tell the gateway which patient each agent's task is about.
    assigner = TaskAssigner(MCP_URL, ServiceTokenProvider(
        kc, "orchestrator", client_secret("orchestrator"), "ehr-mcp", ["task:assign"]))

    def _token(user: str, agent: str, patient: str | None = "jane-doe") -> str:
        if patient:
            assigner.assign(agent, user, patient)
        login = kc.password_login(LOGIN_CLIENT, user,
                                  settings.demo_user_password.get_secret_value(), HUMAN_SCOPES)
        scopes = AGENT_SCOPES[agent]
        steps = run_chain(kc, client_secret, validators, login.access_token, [
            Hop("orchestrator", agent, scopes), Hop(agent, "ehr-mcp", scopes)])
        assert steps[-1].granted, steps[-1].reason
        return steps[-1].token

    return _token


def mcp_client(token: str) -> Client:
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=30)
    return Client(streamable_http_client(MCP_URL, http_client=http))


async def test_no_token_gets_401_with_resource_metadata() -> None:
    response = httpx.post(MCP_URL, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response.status_code == 401
    assert "resource_metadata" in response.headers["www-authenticate"]
    metadata = httpx.get("http://localhost:8010/.well-known/oauth-protected-resource/mcp").json()
    assert metadata["authorization_servers"] == [get_settings().keycloak_issuer]


async def test_token_for_another_service_is_rejected(token_for) -> None:
    settings = get_settings()
    login = KeycloakClient(settings.keycloak_url).password_login(
        LOGIN_CLIENT, "dr.morgana", settings.demo_user_password.get_secret_value(), HUMAN_SCOPES)
    response = httpx.post(MCP_URL, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                          headers={"Authorization": f"Bearer {login.access_token}"})
    assert response.status_code == 401  # aud=orchestrator: no token passthrough


@pytest.mark.parametrize(("agent", "expected"), [
    ("summary-agent", {"get_patient_summary", "get_medications", "get_observations",
                       "get_clinical_notes"}),
    ("scheduling-agent", {"search_patients", "get_discharge_plan", "get_portal_messages"}),
    ("prescription-agent", {"get_patient_summary", "get_medications", "get_allergies",
                            "request_signature"}),
])
async def test_each_agent_sees_only_its_tools(token_for, agent, expected) -> None:
    async with mcp_client(token_for("dr.morgana", agent)) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert names == expected


async def test_physician_and_nurse_see_different_notes(token_for) -> None:
    async with mcp_client(token_for("dr.morgana", "summary-agent")) as client:
        doctor = (await client.call_tool("get_clinical_notes",
                                         {"patient_id": "jane-doe"})).structured_content
    async with mcp_client(token_for("nurse.gawain", "summary-agent")) as client:
        nurse = (await client.call_tool("get_clinical_notes",
                                        {"patient_id": "jane-doe"})).structured_content
    titles = [n["type"]["text"] for n in doctor["notes"]]
    assert "Psychiatry progress note" in titles and doctor["withheld"] == []
    assert len(nurse["notes"]) == len(doctor["notes"]) - 1
    assert "PSY-labelled" in nurse["withheld"][0]["reason"]


async def test_scheduling_agent_is_denied_clinical_notes(token_for) -> None:
    async with mcp_client(token_for("dr.morgana", "scheduling-agent")) as client:
        result = await client.call_tool("get_clinical_notes", {"patient_id": "jane-doe"})
    assert result.is_error and "DENIED by policy" in result.content[0].text


def test_strands_mcp_client_gets_the_same_view(token_for) -> None:
    """The chart agent's model reaches the gateway through Strands' MCP client with the agent's
    delegated token: same filtered tool list, same policy, same withheld records."""
    from strands.tools.mcp import MCPClient

    token = token_for("nurse.gawain", "chart-agent")
    client = MCPClient(url=MCP_URL, headers={"Authorization": f"Bearer {token}"},
                       tool_filters={"rejected": ["search_patients"]})
    with client:
        names = {t.tool_name for t in client.list_tools_sync()}
        notes = client.call_tool_sync("t1", "get_clinical_notes", {"patient_id": "jane-doe"})
        coverage = client.call_tool_sync("t2", "get_coverage", {"patient_id": "jane-doe"})
    assert names == {"get_patient_summary", "get_medications", "get_allergies",
                     "get_observations", "get_clinical_notes"}
    assert notes["status"] == "success"
    assert "PSY-labelled" in str(notes["content"])          # withheld for the nurse
    assert coverage["status"] == "error" and "DENIED" in str(coverage["content"])


async def test_agent_is_bound_to_its_tasks_patient(token_for) -> None:
    async with mcp_client(token_for("dr.morgana", "summary-agent")) as client:
        own = await client.call_tool("get_medications", {"patient_id": "jane-doe"})
        other = await client.call_tool("get_medications", {"patient_id": "pt-6e38fda8"})
    assert not own.is_error
    assert other.is_error and "not assigned to patient pt-6e38fda8" in other.content[0].text


def test_an_agent_cannot_assign_itself_a_patient() -> None:
    from hacknc.common.auth import TokenRequestError

    settings = get_settings()
    kc = KeycloakClient(settings.keycloak_url)
    with pytest.raises(TokenRequestError):           # Keycloak: task:assign is not its scope
        kc.client_credentials("summary-agent", client_secret("summary-agent"),
                              ["aud-ehr-mcp", "task:assign"])
    token = kc.client_credentials("summary-agent", client_secret("summary-agent"),
                                  ["aud-ehr-mcp"]).access_token
    response = httpx.post("http://localhost:8010/tasks/assign",
                          headers={"Authorization": f"Bearer {token}"},
                          json={"agent": "summary-agent", "user": "dr.morgana",
                                "patient_id": "pt-6e38fda8"})
    assert response.status_code in (401, 403), response.text


async def test_patient_reaches_only_her_own_record(token_for) -> None:
    """Even if the portal agent were assigned another patient, the patient rule holds."""
    token = token_for("jane.doe", "portal-agent")
    async with mcp_client(token) as client:
        own = await client.call_tool("get_medications", {"patient_id": "jane-doe"})
        other = await client.call_tool("get_medications", {"patient_id": "pt-87c46349"})
    assert not own.is_error
    assert other.is_error and "DENIED" in other.content[0].text


async def test_task_binding_holds_at_the_disclosure_gateway_too(token_for) -> None:
    """The benefits agent's task is about Jane. Its token for the disclosure gateway is valid,
    but sending Kermit Legros's identity to the payer is refused there as well."""
    settings = get_settings()                        # (token_for: skips if the stack is down)
    kc = KeycloakClient(settings.keycloak_url)
    keys = jwt.PyJWKClient(kc.jwks_uri)
    assigner = TaskAssigner(MCP_URL, ServiceTokenProvider(
        kc, "orchestrator", client_secret("orchestrator"), "ehr-mcp", ["task:assign"]))
    assigner.assign("benefits-agent", "dr.morgana", "jane-doe")
    try:
        login = kc.password_login(LOGIN_CLIENT, "dr.morgana",
                                  settings.demo_user_password.get_secret_value(), HUMAN_SCOPES)
        scopes = ("disclose:prior-auth",)
        steps = run_chain(kc, client_secret,
                          lambda aud: JwtValidator(settings.keycloak_issuer, aud,
                                                   key_provider=keys),
                          login.access_token, [Hop("orchestrator", "benefits-agent", scopes),
                                               Hop("benefits-agent", "disclosure-mcp", scopes)])
        assert steps[-1].granted, steps[-1].reason
        http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {steps[-1].token}"},
                                  timeout=30)
        async with Client(streamable_http_client("http://localhost:8011/mcp",
                                                 http_client=http)) as client:
            own = await client.call_tool("check_eligibility", {"patient_id": "jane-doe"})
            other = await client.call_tool("check_eligibility", {"patient_id": "pt-87c46349"})
    finally:
        assigner.release("benefits-agent", "dr.morgana", "jane-doe")
    assert not own.is_error, own.content
    assert other.is_error and "not assigned to patient pt-87c46349" in other.content[0].text
