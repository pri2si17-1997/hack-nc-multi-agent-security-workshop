"""Generate the Keycloak realm export: ``infra/keycloak/realm-hacknc-hospital.json``.

Run ``make realm`` after editing this file; a unit test fails if the JSON is out of date.

The realm is written as code because the same few patterns repeat many times:

* **Permission scopes** (``patient:read``, ``rx:approve``...) say what may be done. A *role scope
  mapping* ties each one to the human roles allowed to receive it.
* **Audience scopes** (``aud-<service>``) say who may call whom: a client can only obtain a
  token addressed to service X if ``aud-X`` is one of its optional scopes.
* **Clients** are every app, agent and service: each has its own identity and secret.
* **Service accounts** let a service act as *itself* (e.g. writing to the audit log).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hacknc.ehr.records import EhrRepository

REALM = "hacknc-hospital"
ROOT = Path(__file__).resolve().parents[3]
OUTPUT = ROOT / "infra" / "keycloak" / "realm-hacknc-hospital.json"
DATA = ROOT / "data"
BASE_DEFAULT_SCOPES = ["basic", "profile", "roles"]
DEMO_PASSWORD = "hacknc"

PERMISSION_SCOPES: dict[str, str] = {
    # what a human can do (requested at login, narrowed for every agent)
    "agent:delegate": "Let AI agents work on the user's behalf",
    "patient:read": "Read patient charts",
    "discharge:order": "Order a discharge (physicians only)",
    "rx:approve": "Sign prescriptions and discharge summaries (physicians only)",
    "portal:self": "Use the patient portal for your own record (patients only)",
    # what agents can do on the user's behalf
    "sign:request": "Put a draft in the physician's signature queue",
    "portal:write": "Post instructions to the patient portal",
    "disclose:prior-auth": "Send a prior-authorization request to the payer",
    "disclose:registry": "Query the state controlled-substance registry",
    "disclose:prescription": "Send a signed prescription to the patient's pharmacy",
    "disclose:scheduling": "Book a follow-up with the patient's family doctor",
    "disclose:care-transition": "Send the signed discharge summary to the family doctor",
    # services
    "approval:request": "Create or fulfil a signature request (gateways only)",
    "audit:write": "Append events to the audit log (services only)",
    "audit:read": "Read the audit log",
    "task:assign": "Bind an agent to the patient of its current task (coordinator only)",
}

# A permission scope is only granted to users holding one of these roles.
# Scopes not listed here have no role restriction.
DISCLOSURE_SCOPES = ["disclose:prior-auth", "disclose:registry", "disclose:prescription",
                     "disclose:scheduling", "disclose:care-transition"]
SCOPE_ROLES: dict[str, list[str]] = {
    "agent:delegate": ["physician", "nurse"],
    "patient:read": ["physician", "nurse"],
    "discharge:order": ["physician"],
    "rx:approve": ["physician"],
    "sign:request": ["physician"],
    "portal:write": ["physician", "nurse"],
    "approval:request": ["physician", "nurse"],
    "portal:self": ["patient"],
    **{scope: ["physician"] for scope in DISCLOSURE_SCOPES},
}

# Every agent: (what it does, scopes it may hold when calling the EHR / disclosure gateway).
AGENTS: dict[str, tuple[str, list[str], list[str]]] = {
    "medrec-agent": ("Medication reconciliation: home vs hospital vs discharge meds.",
                     ["patient:read"], []),
    "summary-agent": ("Drafts the discharge summary for the family doctor.",
                      ["patient:read", "sign:request"], []),
    "prescription-agent": ("Prepares discharge prescriptions; controlled ones need a signature.",
                           ["patient:read", "sign:request"],
                           ["disclose:registry", "disclose:prescription"]),
    "benefits-agent": ("Coverage and prior authorization with the payer.",
                       ["patient:read"], ["disclose:prior-auth"]),
    "scheduling-agent": ("Books the follow-up with the family doctor.",
                         ["patient:read"], ["disclose:scheduling"]),
    "transitions-agent": ("Sends the signed discharge summary to the family doctor.",
                          [], ["disclose:care-transition"]),
    "instructions-agent": ("Writes plain-language discharge instructions for the patient.",
                           ["patient:read", "portal:write"], []),
    "chart-agent": ("Answers clinicians' questions about a patient (chat box).",
                    ["patient:read"], []),
    "portal-agent": ("Triage of patient portal messages; acts for the patient, decides nothing.",
                     ["portal:self"], []),
}
PARTNERS: dict[str, str] = {
    "payer": "Blue Cross Blue Shield (mock payer)",
    "retail-pharmacy": "Main Street Pharmacy (mock retail pharmacy)",
    "pcp-clinic": "Maple Family Medicine (mock family-doctor clinic)",
    "nc-csrs": "NC Controlled Substances Reporting System (mock state registry)",
}

AUDIENCES = ["hospital-ui", "orchestrator", *AGENTS, "ehr-mcp", "disclosure-mcp",
             "approval-service", "audit-service", *PARTNERS]

HUMAN_SCOPES = ["agent:delegate", "patient:read", "discharge:order", "rx:approve", "portal:self"]
AUDIT_WRITER = ["aud-audit-service", "audit:write"]


# --------------------------------------------------------------------------- building blocks


def _mapper(name: str, mapper_type: str, **config: str) -> dict[str, Any]:
    return {
        "name": name,
        "protocol": "openid-connect",
        "protocolMapper": mapper_type,
        "consentRequired": False,
        "config": {key.replace("_", "."): value for key, value in config.items()},
    }


def _scope(name: str, description: str, mappers: list[dict] | None = None,
           in_token_scope: bool = True) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "protocol": "openid-connect",
        "attributes": {
            "include.in.token.scope": str(in_token_scope).lower(),
            "display.on.consent.screen": "false",
        },
        "protocolMappers": mappers or [],
    }


def _audience_scope(target: str) -> dict[str, Any]:
    mapper = _mapper(f"audience {target}", "oidc-audience-mapper",
                     included_client_audience=target, access_token_claim="true",
                     id_token_claim="false", introspection_token_claim="true")
    return _scope(f"aud-{target}",
                  f"Lets the requesting client obtain tokens addressed to '{target}'",
                  [mapper], in_token_scope=False)


def _builtin_scopes() -> list[dict[str, Any]]:
    """The few standard scopes we rely on, defined explicitly so the realm is self-contained."""
    return [
        _scope("basic", "Subject claim", [
            _mapper("sub", "oidc-sub-mapper", access_token_claim="true",
                    introspection_token_claim="true"),
        ], in_token_scope=False),
        _scope("profile", "Username and name", [
            _mapper("username", "oidc-usermodel-attribute-mapper", user_attribute="username",
                    claim_name="preferred_username", jsonType_label="String",
                    access_token_claim="true", id_token_claim="true",
                    introspection_token_claim="true"),
            _mapper("full name", "oidc-full-name-mapper", access_token_claim="true",
                    id_token_claim="true", introspection_token_claim="true"),
        ], in_token_scope=False),
        _scope("roles", "Realm roles in realm_access.roles", [
            _mapper("realm roles", "oidc-usermodel-realm-role-mapper",
                    claim_name="realm_access.roles", multivalued="true",
                    jsonType_label="String", access_token_claim="true",
                    id_token_claim="false", introspection_token_claim="true"),
        ], in_token_scope=False),
    ]


def _client(client_id: str, description: str, *, optional: list[str] | None = None,
            default_extra: list[str] | None = None, token_exchange: bool = False,
            service_account: bool = False, **overrides: Any) -> dict[str, Any]:
    """A confidential client. Everything is off unless switched on explicitly."""
    client = {
        "clientId": client_id,
        "name": client_id,
        "description": description,
        "enabled": True,
        "publicClient": False,
        "clientAuthenticatorType": "client-secret",
        "secret": f"{client_id}-demo-secret",
        "standardFlowEnabled": False,
        "implicitFlowEnabled": False,
        "directAccessGrantsEnabled": False,
        "serviceAccountsEnabled": service_account,
        "fullScopeAllowed": True,
        "protocol": "openid-connect",
        "attributes": {"standard.token.exchange.enabled": str(token_exchange).lower()},
        "defaultClientScopes": BASE_DEFAULT_SCOPES + (default_extra or []),
        "optionalClientScopes": optional or [],
    }
    client.update(overrides)
    return client


def _resource_server(client_id: str, description: str) -> dict[str, Any]:
    """A service that only *receives* tokens. It exists so tokens can name it as audience."""
    return _client(client_id, description, fullScopeAllowed=False, defaultClientScopes=[])


def _public_test_client() -> dict[str, Any]:
    """``workshop-cli``: lets scripts log in as a user with a password. Never in production."""
    return {
        "clientId": "workshop-cli",
        "name": "Workshop CLI (test harness only)",
        "description": "DEMO/TEST ONLY: password grant so scripts can log in as a user "
                       "without a browser. Never do this in production.",
        "enabled": True,
        "publicClient": True,
        "standardFlowEnabled": False,
        "implicitFlowEnabled": False,
        "directAccessGrantsEnabled": True,
        "serviceAccountsEnabled": False,
        "fullScopeAllowed": True,
        "protocol": "openid-connect",
        "defaultClientScopes": BASE_DEFAULT_SCOPES + ["aud-orchestrator"],
        "optionalClientScopes": HUMAN_SCOPES,
    }


def _user(username: str, first: str, last: str, role: str) -> dict[str, Any]:
    return {
        "username": username,
        "enabled": True,
        "emailVerified": True,
        "firstName": first,
        "lastName": last,
        "email": f"{username}@hacknc-hospital.example",
        "credentials": [{"type": "password", "value": DEMO_PASSWORD, "temporary": False}],
        "realmRoles": [role],
        "requiredActions": [],
    }


def _patient_users() -> list[dict[str, Any]]:
    """A portal login for every patient on the ward, linked to their record by
    ``preferences.portalAccount`` (the EHR gateway resolves it; OPA allows only that record)."""
    repo = EhrRepository.load(DATA / "curated", DATA / "overlay")
    users = []
    for record in sorted(repo.search(), key=lambda r: r.portal_account or ""):
        if record.portal_account:
            name = record.patient["name"][0]
            users.append(_user(record.portal_account, name.get("given", [""])[0],
                               name.get("family", ""), "patient"))
    return users


def _service_account_user(client_id: str) -> dict[str, Any]:
    return {
        "username": f"service-account-{client_id}",
        "enabled": True,
        "serviceAccountClientId": client_id,
        "realmRoles": [],
    }


# --------------------------------------------------------------------------- the realm


def build_realm() -> dict[str, Any]:
    all_agent_scopes = sorted({x for _, ehr, out in AGENTS.values() for x in ehr + out})
    clients = [
        _client(
            "hospital-ui", "Browser app clinicians and admins log into (Authorization Code + PKCE)",
            standardFlowEnabled=True,
            redirectUris=["http://localhost:8000/*"],
            webOrigins=["http://localhost:8000"],
            attributes={
                "pkce.code.challenge.method": "S256",
                "post.logout.redirect.uris": "http://localhost:8000/*",
                # Exchanges the user's token to read the chart, sign, and read the audit log.
                "standard.token.exchange.enabled": "true",
            },
            service_account=True,  # writes its own audit events
            default_extra=["aud-orchestrator", "aud-hospital-ui"],
            optional=HUMAN_SCOPES + ["aud-approval-service", "aud-audit-service", "aud-ehr-mcp",
                                     "audit:read"] + AUDIT_WRITER[1:],
        ),
        _public_test_client(),
        _client("orchestrator",
                "Discharge coordinator: plans the tasks and delegates to agents. No data access.",
                token_exchange=True, service_account=True,
                # aud-ehr-mcp + discharge:order: only to record the discharge when done.
                # task:assign: tell the EHR gateway which patient each agent's task is about.
                # audit:read: check the gateways' own records for blocked attempts.
                optional=[f"aud-{a}" for a in AGENTS] + all_agent_scopes + AUDIT_WRITER
                + ["aud-ehr-mcp", "discharge:order", "task:assign", "audit:read"]),
    ]
    for agent, (description, ehr_scopes, out_scopes) in AGENTS.items():
        optional = ehr_scopes + out_scopes
        if ehr_scopes:
            optional = ["aud-ehr-mcp", *optional]
        if out_scopes:
            optional = ["aud-disclosure-mcp", *optional]
        # service account: each agent writes its own decisions to the audit trail
        clients.append(_client(agent, description, token_exchange=True, service_account=True,
                               optional=optional + AUDIT_WRITER))
    clients += [
        _client("ehr-mcp", "EHR gateway: the only way agents read patient data.",
                token_exchange=True, service_account=True,
                optional=["aud-approval-service", "approval:request"] + AUDIT_WRITER),
        _client("disclosure-mcp", "Disclosure gateway: everything that leaves the hospital.",
                token_exchange=True, service_account=True,
                # aud-ehr-mcp: read the patient's current consent (they change it in the portal)
                optional=[f"aud-{p}" for p in PARTNERS]
                + ["aud-approval-service", "approval:request", "aud-ehr-mcp"] + AUDIT_WRITER),
        _client("approval-service", "Physician signature queue.",
                service_account=True, optional=AUDIT_WRITER),
        _resource_server("audit-service", "Hash-chained audit log (resource server only)."),
    ]
    clients += [_resource_server(p, f"{text}: outside organization, receives data only.")
                for p, text in PARTNERS.items()]
    service_accounts = [c["clientId"] for c in clients if c["serviceAccountsEnabled"]]

    return {
        "realm": REALM,
        "displayName": "HackNC General Hospital",
        "enabled": True,
        "sslRequired": "none",
        "registrationAllowed": False,
        "loginWithEmailAllowed": False,
        "accessTokenLifespan": 900,
        "roles": {"realm": [
            {"name": "physician", "description": "Attending physician: full chart, orders "
                                                 "discharges, signs prescriptions and summaries"},
            {"name": "nurse", "description": "Nurse: chart access except sensitive records; "
                                             "cannot order or sign"},
            {"name": "admin", "description": "Platform admin: demo controls, audit and "
                                             "disclosures; no clinical data"},
            {"name": "patient", "description": "Patient: the portal, for their own record "
                                               "only (messages, requests, consent)"},
        ]},
        "users": [
            _user("dr.morgana", "Morgana", "Pendragon", "physician"),
            _user("nurse.gawain", "Gawain", "Orkney", "nurse"),
            _user("admin.percival", "Percival", "Grail", "admin"),
        ] + _patient_users() + [_service_account_user(c) for c in service_accounts],
        "clientScopes": _builtin_scopes()
        + [_scope(name, text) for name, text in PERMISSION_SCOPES.items()]
        + [_audience_scope(target) for target in AUDIENCES],
        "scopeMappings": [{"clientScope": s, "roles": r} for s, r in SCOPE_ROLES.items()],
        "defaultDefaultClientScopes": BASE_DEFAULT_SCOPES,
        "defaultOptionalClientScopes": [],
        "clients": clients,
    }


def render() -> str:
    return json.dumps(build_realm(), indent=2) + "\n"


def main() -> None:
    OUTPUT.write_text(render())
    print(f"wrote {OUTPUT}")


if __name__ == "__main__":
    main()
