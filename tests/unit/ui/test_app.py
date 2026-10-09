"""The hospital UI with every backend faked (no network): login, role guards, pages, runs."""

from __future__ import annotations

import json
import time
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from hacknc.common.auth import JwtValidator, TokenClaims, TokenSet, TokenValidationError
from hacknc.common.http_api import ApiResult
from hacknc.ui.app import create_app
from hacknc.ui.context import UiDependencies
from hacknc.ui.oidc import PendingLogin
from hacknc.ui.otlp import SpanRecord
from hacknc.ui.routes import admin as admin_routes
from hacknc.ui.routes import clinical
from hacknc.ui.routes import portal as portal_routes
from hacknc.ui.runs import RunHub
from hacknc.ui.sessions import SessionStore
from hacknc.ui.settings import UiSettings
from tests.conftest import ISSUER
from tests.fakes import RecordingAudit

JANE = {"id": "jane-doe", "name": "Jane Doe", "gender": "female", "age": 60, "mrn": "2c405456",
        "birthDate": "1966-08-13",
        "admission": {"reason": "Community-acquired pneumonia", "since": "2026-09-30",
                      "location": "4 West, bed 12", "attending": "Dr. Morgana Pendragon"}}
PSY = {"meta": {"security": [{"code": "PSY"}]}}


class FakeOidc:
    """Every login succeeds as ``username`` with ``role``."""

    def __init__(self, make_token) -> None:
        self.make_token = make_token
        self.username, self.role = "dr.morgana", "physician"
        self.token_ttl, self.refreshed, self.fail_finish = 600, 0, False
        self.hints: list[str | None] = []

    def _tokens(self) -> TokenSet:
        scope = {"physician": "agent:delegate patient:read discharge:order rx:approve",
                 "nurse": "agent:delegate patient:read", "admin": "",
                 "patient": "portal:self", "visitor": ""}[self.role]
        access = self.make_token(aud=["orchestrator", "hospital-ui"], azp="hospital-ui",
                                 preferred_username=self.username, scope=scope,
                                 realm_access={"roles": [self.role]},
                                 exp=int(time.time()) + self.token_ttl)
        return TokenSet(access_token=access, refresh_token="refresh-1", id_token="id-1")

    def start(self, login_hint=None) -> PendingLogin:
        self.hints.append(login_hint)
        return PendingLogin("state-1", "nonce-1", "verifier-1", "http://kc.test/auth?x=1")

    def finish(self, code, code_verifier, expected_nonce):
        if self.fail_finish:
            raise TokenValidationError("bad id token")
        return self._tokens(), TokenClaims({"preferred_username": self.username,
                                            "name": self.username.title()})

    def refresh(self, refresh_token):
        self.refreshed += 1
        self.token_ttl = 600
        return self._tokens()

    def logout_url(self, id_token, post_logout_redirect_uri):
        return f"http://kc.test/logout?hint={id_token}"


class FakeKeycloak:
    def exchange_token(self, client_id, secret, subject_token, audience, scopes=()):
        return TokenSet(access_token=f"{client_id}->{audience}")


class FakeApprovals:
    def __init__(self) -> None:
        self.flags = {"agent.medrec-agent": True, "scenario.oversharing": False,
                      "break_glass_enabled": True}
        self.decisions, self.grants = [], []

    def break_glass(self, token, patient_id, justification):
        if len(justification) < 15:
            return ApiResult(403, {"reason": "a justification of at least 15 characters"})
        self.grants.append((patient_id, justification))
        return ApiResult(201, {"id": "g1", "patient_id": patient_id})

    def decide(self, token, request_id, approve, note=""):
        self.decisions.append((request_id, approve))
        if request_id == "deny-me":
            return ApiResult(403, {"reason": "only physicians may sign"})
        return ApiResult(200, {"id": request_id, "status": "approved"})

    def get_flags(self, admin_key):
        return ApiResult(200, dict(self.flags))

    def set_flags(self, admin_key, flags):
        self.flags.update(flags)
        return ApiResult(200, dict(self.flags))


class FakeAuditApi:
    def events(self, token, limit=100, **filters):
        return ApiResult(200, {"total": 0, "entries": []})

    def verify(self, token):
        return ApiResult(200, {"ok": False, "broken_at": 1, "problem": "modified",
                               "checked": 0, "head_hash": "0" * 64})


@pytest.fixture
def oidc(make_token) -> FakeOidc:
    return FakeOidc(make_token)


@pytest.fixture
def backend(monkeypatch):
    """Fake coordinator + EHR gateway + audit reads used by the clinical routes."""
    calls: list[tuple] = []

    async def coordinator(ctx, session, method, path, json=None):
        calls.append((method, path, json))
        if path == "/discharges":
            return 200, {"jane-doe": "awaiting_signatures"}
        if path.endswith("/start"):
            return 200, {"status": "awaiting_signatures", "tasks": []}
        if path.endswith("/signed"):
            return 200, {"status": "completed", "tasks": []}
        if path == "/portal/messages":
            return 200, {"ok": True, "reply": "Thanks, Jane. A doctor will review it.",
                         "request": {"id": "req-1", "kind": "medication_change"}}
        if path.startswith("/portal/requests/"):
            return 200, {"request": {"id": "req-1", "status": "accepted"},
                         "hint": "Added to the discharge plan"}
        if path == "/chat":
            if "Discharge" in json["message"]:
                return 200, {"reply": "Started", "by": "⚙ rules",
                             "discharge": {"status": "awaiting_signatures", "tasks": []}}
            return 200, {"reply": "**Allergies:** Penicillin", "by": "⚙ rules",
                         "discharge": {"status": "not_started", "tasks": []}}
        return 200, {"status": "not_started", "tasks": []}

    async def ehr(ctx, session, requested, scopes=("patient:read",)):
        out = []
        for tool, args in requested:
            calls.append(("ehr", tool, args, tuple(scopes)))
            if tool == "get_my_record":
                out.append({"patient": JANE, "consent": {"shareSensitiveWithPcp": False,
                                                         "note": "No consent on file"},
                            "pharmacy": "Main Street Pharmacy"})
            elif tool == "get_patient_requests":
                out.append({"requests": [{"id": "req-1", "kind": "refill", "status": "open",
                                          "summary": "Refill of oxycodone, please"}]})
            elif tool == "update_consent":
                out.append({"consent": {"shareSensitiveWithPcp": args[
                    "share_sensitive_with_pcp"], "note": "Patient consented in the portal"}})
            elif tool == "search_patients":
                out.append({"patients": [JANE]})
            elif tool == "get_patient_summary":
                out.append({"patient": JANE, "admission": None, "vital_signs": [],
                            "active_conditions": [{"code": {"text": "Pneumonia"}}],
                            "withheld": [{"type": "Condition", "reason": "PSY-labelled"}]})
            elif tool == "get_clinical_notes":
                out.append({"notes": [{"type": {"text": "Psychiatry note"}, "date": "2026-08-20",
                                       "author": "Dr. Okafor", "text": "…", **PSY}],
                            "withheld": []})
            else:
                key = {"get_medications": "medications", "get_allergies": "allergies",
                       "get_observations": "observations", "get_portal_messages": "messages"}[tool]
                out.append({key: [], "withheld": [], "posted_instructions": []})
        return out

    async def audit_events(ctx, session, **filters):
        if filters.get("patient_id") and not filters.get("action"):
            return {"ok": True, "entries": [{
                "seq": 9, "recorded_at": "2026-10-03T10:01:02", "patient_id": "jane-doe",
                "action": "mcp.tool_call", "decision": "deny", "severity": "warning",
                "reason": "scheduling-agent may not read notes", "actor_user": "dr.morgana",
                "actor_role": "physician", "agent_chain": ["scheduling-agent", "ehr-mcp"],
                "writer": "ehr-mcp"}]}
        if filters.get("action") == "disclosure.sent":
            return {"ok": True, "entries": [{
                "seq": 5, "recorded_at": "2026-10-03T10:00:00", "patient_id": "jane-doe",
                "decision": "allow", "reason": "sent", "agent_chain": ["benefits-agent"],
                "actor_user": "dr.morgana", "trace_id": "t",
                "details": {"destination_name": "Blue Cross Blue Shield",
                            "purpose": "prior_authorization", "fields": ["name"],
                            "withheld": ["DocumentReference"]}}]}
        return {"ok": True, "entries": []}

    monkeypatch.setattr(clinical, "coordinator", coordinator)
    monkeypatch.setattr(clinical, "ehr", ehr)
    monkeypatch.setattr(portal_routes, "coordinator", coordinator)
    monkeypatch.setattr(portal_routes, "ehr", ehr)
    monkeypatch.setattr(clinical, "audit_events", audit_events)
    monkeypatch.setattr(admin_routes, "audit_events", audit_events)
    return calls


@pytest.fixture
def deps(oidc, key_provider) -> UiDependencies:
    return UiDependencies(
        settings=UiSettings(ui_session_secret=SecretStr("test-secret"), demo_admin_key="k"),
        oidc=oidc, keycloak=FakeKeycloak(),
        validators=lambda aud: JwtValidator(ISSUER, aud, key_provider=key_provider),
        secrets=lambda client_id: "s", sessions=SessionStore(), approvals=FakeApprovals(),
        audit_reader=FakeAuditApi(), audit=RecordingAudit(), runs=RunHub(grace_seconds=0.01))


@pytest.fixture
def client(deps, backend) -> TestClient:
    return TestClient(create_app(deps), follow_redirects=False)


def log_in(client: TestClient, oidc: FakeOidc, as_: str = "dr.morgana",
           role: str = "physician") -> str:
    oidc.username, oidc.role = as_, role
    client.get("/login")
    response = client.get("/auth/callback", params={"code": "c", "state": "state-1"})
    assert response.status_code == 303, response.text
    return response.headers["location"]


def follow(client: TestClient, started: dict) -> dict:
    text = client.get(f"/api/runs/{started['run_id']}/events").text
    events = [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data:")]
    return next(e for e in events if e["type"] == "answer")


# ------------------------------------------------------------------ login and roles


def test_landing_lists_three_personas(client) -> None:
    page = client.get("/").text
    for username in ("dr.morgana", "nurse.gawain", "admin.percival"):
        assert f"/login?persona={username}" in page


def test_landing_lets_you_pick_any_patient(client, oidc) -> None:
    page = client.get("/").text
    assert '<select id="patient-login" name="persona">' in page
    for username in ("jane.doe", "jeff.heathcote", "kermit.legros"):
        assert f'<option value="{username}">' in page
    client.get("/login", params={"persona": "jeff.heathcote"})
    assert oidc.hints == ["jeff.heathcote"]                      # Keycloak pre-fills it


def test_login_lands_on_the_right_home(client, oidc) -> None:
    assert log_in(client, oidc) == "/ward"
    client.get("/logout")
    assert log_in(client, oidc, "admin.percival", "admin") == "/admin"


@pytest.mark.parametrize("path", ["/ward", "/ward/jane-doe/chart", "/admin"])
def test_pages_require_login(client, path) -> None:
    assert client.get(path).headers["location"] == "/"


def test_role_guards(client, oidc) -> None:
    log_in(client, oidc, "nurse.gawain", "nurse")
    assert client.get("/admin").headers["location"] == "/ward"
    assert client.post("/api/admin/flags", json={}).status_code == 403
    client.get("/logout")
    log_in(client, oidc, "admin.percival", "admin")
    assert client.get("/ward").headers["location"] == "/admin"
    assert client.post("/api/chat", json={"patient_id": "jane-doe",
                                          "message": "hi"}).status_code == 403


def test_callback_with_wrong_state_is_rejected(client) -> None:
    client.get("/login")
    response = client.get("/auth/callback", params={"code": "c", "state": "forged"})
    assert response.status_code == 400 and "did not match" in response.text


def test_failed_login_is_audited(client, deps) -> None:
    client.get("/auth/callback", params={"error": "access_denied"})
    assert deps.audit.events[-1].decision == "deny"


def test_unknown_persona_is_not_passed_as_hint(client, oidc) -> None:
    client.get("/login", params={"persona": "<script>"})
    assert oidc.hints == [None]


def test_expiring_token_is_refreshed(client, oidc) -> None:
    oidc.token_ttl = 30
    log_in(client, oidc)
    assert client.get("/ward").status_code == 200
    assert oidc.refreshed == 1


def test_logout_ends_session(client, oidc) -> None:
    log_in(client, oidc)
    response = client.get("/logout")
    assert parse_qs(urlparse(response.headers["location"]).query)["hint"] == ["id-1"]
    assert client.get("/ward").status_code == 303


# ------------------------------------------------------------------ clinical pages


def test_ward_shows_census_status_chat_and_activity(client, oidc) -> None:
    log_in(client, oidc)
    page = client.get("/ward").text
    assert "Jane Doe" in page and "Needs your signature" in page
    assert 'id="chat-form"' in page and 'id="live-map"' in page


def test_old_pages_redirect_to_the_ward(client, oidc) -> None:
    log_in(client, oidc)
    assert client.get("/patients").headers["location"] == "/ward"
    assert client.get("/patients/jane-doe").headers["location"] == "/ward?patient=jane-doe"


def test_nurse_gets_the_same_ward_screen(client, oidc) -> None:
    log_in(client, oidc, "nurse.gawain", "nurse")
    page = client.get("/ward").text
    assert 'id="chat-form"' in page and "Discharge {first}" in page


def test_access_log_lists_agent_and_human_access(client, oidc) -> None:
    log_in(client, oidc)
    [entry] = client.get("/api/access-log/jane-doe").json()["entries"]
    assert entry["who"] == "dr.morgana" and entry["via"] == ["scheduling-agent", "ehr-mcp"]
    assert entry["what"] == "read" and entry["decision"] == "deny"


def test_chart_error_does_not_reflect_html(client, oidc, monkeypatch) -> None:
    from hacknc.agents.runtime import ToolDenied

    async def unknown(ctx, session, requested, scopes=("patient:read",)):
        raise ToolDenied(f"no patient with id '{requested[0][1]['patient_id']}'")

    monkeypatch.setattr(clinical, "ehr", unknown)
    log_in(client, oidc)
    response = client.get("/ward/<img src=x onerror=alert(1)>/chart")
    assert response.status_code == 503
    assert "<img" not in response.text and "&lt;img" in response.text


def test_chart_shows_labels_and_withheld_records(client, oidc) -> None:
    log_in(client, oidc)
    page = client.get("/ward/jane-doe/chart").text
    assert 'data-tab="access"' in page
    assert "🔒 PSY" in page
    assert "Condition withheld: PSY-labelled" in page


def test_disclosures_come_from_the_audit_trail(client, oidc) -> None:
    log_in(client, oidc)
    row = client.get("/api/disclosures/jane-doe").json()["disclosures"][0]
    assert row["destination"] == "Blue Cross Blue Shield"
    assert row["purpose"] == "prior authorization" and row["withheld"] == ["DocumentReference"]


# ------------------------------------------------------------------ runs


def test_chat_sign_and_ask_are_runs(client, oidc, backend) -> None:
    with client:  # one event loop: runs finish in background tasks
        log_in(client, oidc)
        started = client.post("/api/chat", json={"patient_id": "jane-doe",
                                                 "message": "Discharge Jane Doe"}).json()
        assert started["trace_url"].startswith("http://localhost:3000/explore")
        assert follow(client, started)["checklist"]["status"] == "awaiting_signatures"

        signed = follow(client, client.post("/api/signatures/rx1",
                                            json={"patient_id": "jane-doe"}).json())
        assert signed["ok"] and signed["checklist"]["status"] == "completed"
        assert ("POST", "/discharges/jane-doe/signed",
                {"signature_id": "rx1", "decision": "approved"}) in backend

        answer = follow(client, client.post("/api/chat", json={"patient_id": "jane-doe",
                                                                "message": "allergies?"}).json())
        assert "Penicillin" in answer["reply"]


def test_only_the_person_who_started_a_run_can_follow_it(client, oidc, deps, backend) -> None:
    with client:
        log_in(client, oidc)
        started = client.post("/api/chat", json={"patient_id": "jane-doe",
                                                 "message": "allergies?"}).json()
        assert "Penicillin" in follow(client, started)["reply"]
        log_in(client, oidc, "jane.doe", "patient")      # someone else, same browser
        assert client.get(f"/api/runs/{started['run_id']}/events").status_code == 404
    assert not deps.runs._tasks                          # background work all finished


def test_each_step_links_to_its_span_in_grafana(client, oidc, deps) -> None:
    with client:
        log_in(client, oidc)
        deps.runs.start("t1", user="dr.morgana")
        deps.runs.add_spans([SpanRecord("t1", "span42", "", "ui.ask", "hospital-ui", 1, 2,
                                        {"user.name": "dr.morgana", "question": "hi"})])
        client.portal.call(deps.runs.finish, "t1", {"reply": "ok"})   # needs the app's loop
        text = client.get("/api/runs/t1/events").text
    [step] = [json.loads(line[6:]) for line in text.splitlines()
              if line.startswith("data:") and '"step"' in line]
    panes = json.loads(parse_qs(urlparse(step["trace_url"]).query)["panes"][0])
    assert panes["t"]["queries"][0]["query"] == "t1"
    assert panes["t"]["panelsState"] == {"trace": {"spanId": "span42"}}


def test_refused_signature_does_not_continue(client, oidc, backend) -> None:
    with client:
        log_in(client, oidc, "nurse.gawain", "nurse")
        result = follow(client, client.post("/api/signatures/deny-me",
                                            json={"patient_id": "jane-doe"}).json())
    assert not result["ok"] and "only physicians" in result["reason"]
    assert not any(path.endswith("/signed") for _, path, _ in backend)


def test_spans_from_the_collector_become_steps(client, deps) -> None:
    deps.runs.start("abc")
    export = {"resourceSpans": [{
        "resource": {"attributes": [{"key": "service.name",
                                     "value": {"stringValue": "orchestrator"}}]},
        "scopeSpans": [{"spans": [{
            "traceId": "abc", "spanId": "aa", "name": "orchestrator plan",
            "startTimeUnixNano": "1", "endTimeUnixNano": "2",
            "attributes": [{"key": "plan.reason", "value": {"stringValue": "checklist"}}],
        }]}]}]}
    assert client.post("/otlp/v1/traces", json=export).status_code == 200
    assert deps.runs.get("abc").events[0]["title"] == "Coordinator plans: checklist"


# ------------------------------------------------------------------ admin


def test_admin_switches_are_stored_and_audited(client, oidc, deps) -> None:
    log_in(client, oidc, "admin.percival", "admin")
    page = client.get("/admin").text
    assert "Medication reconciliation" in page and "Oversharing" in page
    flags = client.post("/api/admin/flags", json={"scenario.oversharing": True}).json()
    assert flags["scenario.oversharing"] is True
    assert deps.audit.events[-1].action == "demo.control_changed"


def test_admin_audit_and_verify(client, oidc) -> None:
    log_in(client, oidc, "admin.percival", "admin")
    assert client.get("/admin/audit").status_code == 200
    body = client.post("/api/admin/audit/verify").json()
    assert body["ok"] and body["chain"]["broken_at"] == 1


class NightAuditApi:
    """The audit service after the finale's night (entries from test_incident)."""

    def __init__(self, entries, chain=None) -> None:
        self.entries, self.calls = entries, []
        self.chain = chain or {"ok": True, "checked": len(entries), "broken_at": None,
                               "problem": "", "head_hash": "a" * 64}

    def events(self, token, limit=100, **filters):
        self.calls.append(filters)
        rows = sorted(self.entries, key=lambda e: -e["seq"])
        if filters.get("action"):
            rows = [e for e in rows if e["action"] == filters["action"]]
        if filters.get("after") is not None:
            rows = [e for e in rows if e["seq"] > filters["after"]]
        if filters.get("before") is not None:
            rows = [e for e in rows if e["seq"] < filters["before"]]
        return ApiResult(200, {"entries": rows[:limit], "total": len(self.entries)})

    def verify(self, token):
        return ApiResult(200, self.chain)


class FakePartners:
    def __init__(self, received=()) -> None:
        self.received_rows, self.resets = list(received), 0

    def received(self, admin_key, partner=None):
        return ApiResult(200, {"received": self.received_rows})

    def reset(self, admin_key):
        self.resets += 1
        return ApiResult(204)


@pytest.fixture
def night(deps):
    from tests.unit.ui.test_incident import NIGHT, RECEIVED
    deps.audit_reader = NightAuditApi(NIGHT)
    deps.partners = FakePartners(RECEIVED)
    return deps


def test_incident_report_answers_the_five_questions(night, backend, oidc) -> None:
    client = TestClient(create_app(night), follow_redirects=False)
    log_in(client, oidc, "admin.percival", "admin")
    page = client.get("/admin/incident").text
    assert "✓ Record verified" in page
    for question in ("Who acted?", "On whose authority?", "What did they see?",
                     "What left the hospital?", "What was stopped?"):
        assert question in page
    assert "pt-87c46349" in page and "Benefits &amp; prior auth" in page
    assert "OPA decision log" in page and "Reviewed" in page
    assert "sent by an agent whose task was suspended" in page
    # Since the last reset: the period starts after the finale-prepared mark (#1).
    assert {"after": 1, "before": None} in night.audit_reader.calls
    body = client.get("/api/admin/incident/jane-doe", params={"period": "all"}).json()
    assert body["ok"] and body["report"]["period"]["since_seq"] is None
    assert body["report"]["stopped"]["blocked"][0]["review"]["who"] == "dr.morgana"
    assert body["patients"][0]["id"] == "jane-doe"


def test_incident_report_shows_where_the_record_was_altered(night, backend, oidc) -> None:
    night.audit_reader.chain = {"ok": False, "checked": 5, "broken_at": 6,
                                "problem": "entry #6 was modified after it was written"}
    client = TestClient(create_app(night), follow_redirects=False)
    log_in(client, oidc, "admin.percival", "admin")
    page = client.get("/admin/incident").text
    assert "✕ Record altered at entry #6" in page and "part of this report" in page


def test_incident_report_is_admin_only_and_survives_a_dead_audit(night, backend, oidc) -> None:
    client = TestClient(create_app(night), follow_redirects=False)
    log_in(client, oidc)
    assert client.get("/admin/incident").headers["location"] == "/ward"
    assert client.get("/api/admin/incident/jane-doe").status_code == 403
    log_in(client, oidc, "admin.percival", "admin")
    night.audit_reader.events = lambda token, **f: ApiResult(503, {"reason": "unreachable"})
    page = client.get("/admin/incident")
    assert page.status_code == 200 and "could not be read: unreachable" in page.text
    assert client.get("/api/admin/incident/jane-doe", params={"period": "x"}).status_code == 422


def test_prepare_finale_resets_and_stages_wrong_patient(night, backend, oidc,
                                                        monkeypatch) -> None:
    async def services(settings, key):
        return {"checklists": True, "patient portal": True}

    monkeypatch.setattr(admin_routes, "reset_services", services)
    night.approvals.reset = lambda key: ApiResult(204)
    client = TestClient(create_app(night), follow_redirects=False)
    log_in(client, oidc, "admin.percival", "admin")
    night.approvals.flags["scenario.rogue_agent"] = True
    body = client.post("/api/admin/prepare-finale").json()
    assert body["ok"], body
    assert night.approvals.flags["scenario.wrong_patient"] is True
    assert night.approvals.flags["scenario.rogue_agent"] is False
    assert night.approvals.flags["ai.model"] is True
    assert night.partners.resets == 1
    assert night.audit.events[-1].action == "demo.finale_prepared"
    page = " ".join(client.get("/admin").text.split())
    assert "Reset &amp; stage Wrong patient" in page                # says what it stages
    assert "never legitimate" in page                                # and why


# ------------------------------------------------------------------ patient portal


def test_patient_lands_on_the_portal_and_nowhere_else(client, oidc) -> None:
    assert log_in(client, oidc, "jane.doe", "patient") == "/portal"
    page = client.get("/portal").text
    assert "Hi Jane" in page and 'id="portal-form"' in page and 'id="consent"' in page
    assert client.get("/ward").headers["location"] == "/portal"
    assert client.get("/admin").headers["location"] == "/portal"
    assert client.post("/api/chat", json={"patient_id": "jane-doe",
                                          "message": "Discharge me"}).status_code == 403


def test_clinicians_cannot_use_the_portal(client, oidc) -> None:
    log_in(client, oidc)
    assert client.get("/portal").headers["location"] == "/ward"
    assert client.post("/api/portal/messages", json={"message": "hi"}).status_code == 403


def test_portal_message_uses_the_linked_record_not_the_browser(client, oidc, backend) -> None:
    log_in(client, oidc, "jane.doe", "patient")
    result = client.post("/api/portal/messages",
                         json={"message": "Please raise my oxycodone to 10 mg"}).json()
    assert result["ok"] and "review" in result["reply"]
    sent = next(c for c in backend if c[:2] == ("POST", "/portal/messages"))
    assert sent[2]["patient_id"] == "jane-doe"            # from get_my_record, server side
    assert all(c[3] == ("portal:self",) for c in backend if c[0] == "ehr")


def test_patient_changes_consent(client, oidc, backend) -> None:
    log_in(client, oidc, "jane.doe", "patient")
    saved = client.post("/api/portal/consent", json={"share_sensitive_with_pcp": True}).json()
    assert saved["consent"]["shareSensitiveWithPcp"] is True


def test_physician_decides_a_request_as_a_run(client, oidc, backend) -> None:
    with client:
        log_in(client, oidc)
        assert client.get("/api/requests/jane-doe").json()["requests"][0]["id"] == "req-1"
        started = client.post("/api/requests/jane-doe/req-1", json={"accept": True}).json()
        result = follow(client, started)
        assert result["ok"] and result["request"]["status"] == "accepted"
        assert ("POST", "/portal/requests/jane-doe/req-1/decide",
                {"accept": True, "note": ""}) in backend


def test_user_without_a_hospital_role_gets_an_explanation_not_a_loop(client, oidc) -> None:
    assert log_in(client, oidc, "visitor", "visitor") == "/?error=no-role"
    page = client.get("/?error=no-role")
    assert page.status_code == 200 and "has no role in this hospital app" in page.text
    assert client.get("/ward").headers["location"] == "/?error=no-role"


def test_nurse_can_break_the_glass_with_a_justification(client, oidc, deps) -> None:
    log_in(client, oidc, "nurse.gawain", "nurse")
    drawer = client.get("/ward/jane-doe/chart").text
    assert 'data-break-glass="jane-doe"' in drawer          # withheld records + switch on
    short = client.post("/api/break-glass/jane-doe", json={"justification": "now"}).json()
    assert not short["ok"] and "15 characters" in short["reason"]
    granted = client.post("/api/break-glass/jane-doe", json={
        "justification": "Patient unresponsive, need full history"}).json()
    assert granted["ok"] and deps.approvals.grants == [
        ("jane-doe", "Patient unresponsive, need full history")]


def test_static_files_are_revalidated(client) -> None:
    # After a rebuild the browser must fetch the new CSS/JS, not keep a stale copy.
    assert client.get("/static/app.css").headers["cache-control"] == "no-cache"


def test_static_urls_carry_a_content_version(client) -> None:
    page = client.get("/").text
    assert '/static/app.css?v=' in page and 'href="/static/app.css"' not in page
