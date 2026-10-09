"""Disclosure gateway in-process: OPA, partners and the signature queue are fakes.

The real policy is tested by `make opa-test`; here we check the gateway does what the
decision says: send only allowed fields, strip records, refuse, record."""

from __future__ import annotations

from pathlib import Path

import pytest
from mcp import Client

from hacknc.common.auth import TokenClaims, TokenSet
from hacknc.common.http_api import ApiResult
from hacknc.common.mcp_server import Caller
from hacknc.disclosure_mcp.server import DisclosureDeps, DisclosureMcpServer
from hacknc.ehr.records import EhrRepository
from tests.fakes import RecordingAudit

ROOT = Path(__file__).resolve().parents[3]


class FakeOpa:
    def __init__(self):
        self.inputs, self.decision = [], None

    def query(self, policy, input_doc):
        self.inputs.append(input_doc)
        if self.decision is not None:
            return self.decision
        psych = [{"id": r["id"], "type": r["type"], "reason": "PSY-labelled record: no consent"}
                 for r in input_doc["resources"] if r["labels"]]
        allowed = {"eligibility": {"member_id", "name", "dob"},
                   "prior_authorization": {"member_id", "name", "dob", "service",
                                           "diagnosis_codes", "evidence"},
                   "prescription": {"name", "dob", "prescription", "signature"},
                   "care_transition": {"name", "dob", "document_type", "summary"}}[
            input_doc["purpose"]]
        return {"allow": True, "reason": "allowed",
                "allowed_fields": sorted(f for f in input_doc["fields"] if f in allowed),
                "stripped_fields": [{"field": f, "reason": "not needed"}
                                    for f in input_doc["fields"] if f not in allowed],
                "stripped_resources": psych}


class FakePartners:
    def __init__(self):
        self.sent = []

    def send(self, purpose, token, payload):
        self.sent.append((purpose, token, payload))
        return ApiResult(200, {"status": "approved", "authorization": "PA-1"})


class FakeApprovals:
    def __init__(self, ok=True):
        self.ok, self.consumed = ok, []

    def consume(self, token, signature_id, content, patient_id, kind):
        self.consumed.append((signature_id, content, patient_id, kind))
        if not self.ok:
            return ApiResult(403, {"reason": "content differs from what the physician signed"})
        return ApiResult(200, {"fingerprint": "f" * 64, "decided_by": "dr.morgana",
                               "decided_at": 1.0})


class FakeKeycloak:
    def exchange_token(self, client_id, secret, subject_token, audience, scopes=()):
        return TokenSet(access_token=f"token-for-{audience}")


def caller(agent: str) -> Caller:
    return Caller(TokenClaims({"azp": agent, "preferred_username": "dr.morgana",
                               "realm_access": {"roles": ["physician"]}, "scope": ""}), "tok")


@pytest.fixture(scope="module")
def repo() -> EhrRepository:
    return EhrRepository.load(ROOT / "data" / "curated", ROOT / "data" / "overlay")


@pytest.fixture
def env(repo):
    deps = DisclosureDeps(repo=repo, opa=FakeOpa(), keycloak=FakeKeycloak(),
                          secrets=lambda c: "s", partners=FakePartners(),
                          approvals=FakeApprovals(), audit=RecordingAudit(),
                          caller_source=lambda: caller("benefits-agent"))
    return DisclosureMcpServer(deps), deps


async def call(server, name, **args):
    async with Client(server) as client:
        return await client.call_tool(name, args)


@pytest.mark.anyio
async def test_prior_auth_sends_only_allowed_fields_and_records(env) -> None:
    server, deps = env
    result = await call(server, "request_prior_authorization", patient_id="jane-doe",
                        service="Home oxygen", diagnosis_codes=["J18.9"],
                        evidence_ids=["ov-jd-obs-spo2-walk", "ov-jd-note-psych"],
                        extra={"clinical_notes": "everything"})
    assert not result.is_error, result.content
    purpose, token, payload = deps.partners.sent[-1]
    assert (purpose, token) == ("prior_authorization", "token-for-payer")
    assert "clinical_notes" not in payload
    assert len(payload["evidence"]) == 1 and "86 %" in payload["evidence"][0]
    assert payload["name"] == "Jane Doe" and payload["member_id"] == "BCB-447-2190"
    withheld = result.structured_content["withheld"]
    assert {w.get("field") or w.get("type") for w in withheld} == {"clinical_notes",
                                                                   "DocumentReference"}
    event = deps.audit.events[-1]
    assert event.action == "disclosure.sent" and event.details["destination"] == "payer"
    assert isinstance(event.details["scopes"], list)             # the authority it acted on
    assert event.agent_chain == ["benefits-agent", "disclosure-mcp", "payer"]


@pytest.mark.anyio
async def test_policy_refusal_sends_nothing(env) -> None:
    server, deps = env
    deps.opa.decision = {"allow": False, "reason": "payer is switched off by the admin"}
    result = await call(server, "check_eligibility", patient_id="jane-doe")
    assert result.is_error and "switched off" in result.content[0].text
    assert deps.partners.sent == []
    assert deps.audit.events[-1].action == "disclosure.blocked"


@pytest.mark.anyio
async def test_tampered_prescription_is_never_sent(env) -> None:
    server, deps = env
    deps.approvals.ok = False
    result = await call(server, "send_prescription", patient_id="jane-doe", signature_id="s1",
                        prescription={"drug": "oxycodone", "quantity": 300})
    assert result.is_error and "differs" in result.content[0].text
    assert deps.partners.sent == []


@pytest.mark.anyio
async def test_signed_prescription_carries_the_signature(env) -> None:
    server, deps = env
    rx = {"patient_id": "jane-doe", "drug": "oxycodone", "dose": "5 mg", "quantity": 20}
    await call(server, "send_prescription", patient_id="jane-doe", signature_id="s1",
               prescription=rx)
    assert deps.approvals.consumed == [("s1", rx, "jane-doe", "prescription")]
    _, token, payload = deps.partners.sent[-1]
    assert token == "token-for-retail-pharmacy"
    assert payload["prescription"] == rx and payload["signature"]["signer"] == "dr.morgana"


@pytest.mark.anyio
async def test_summary_sections_from_sensitive_records_are_withheld(env) -> None:
    server, deps = env
    summary = {"sections": [
        {"title": "Hospital course", "text": "Better", "source": "ov-jd-note-day3"},
        {"title": "Behavioral health", "text": "MDD", "source": "ov-jd-cond-mdd"},
        {"title": "Follow-up", "text": "Dr. Lee", "source": None}]}
    await call(server, "send_discharge_summary", patient_id="jane-doe", signature_id="s2",
               summary=summary)
    _, _, payload = deps.partners.sent[-1]
    titles = [s["title"] for s in payload["summary"]["sections"]]
    assert titles == ["Hospital course", "Follow-up"]
    assert payload["summary"]["withheld_sections"] == 1
    # The signature must have been given for Jane's discharge summary, not anything else.
    assert deps.approvals.consumed[-1][2:] == ("jane-doe", "discharge_summary")


@pytest.mark.anyio
async def test_a_sensitive_diagnosis_code_is_as_protected_as_its_record(env) -> None:
    """A model choosing the codes could add Jane's depression (F32.1): it never leaves."""
    server, deps = env
    result = await call(server, "request_prior_authorization", patient_id="jane-doe",
                        service="Home oxygen", diagnosis_codes=["J18.9", "F32.1"],
                        evidence_ids=["ov-jd-obs-spo2-walk"])
    assert not result.is_error, result.content
    payload = deps.partners.sent[-1][2]
    assert payload["diagnosis_codes"] == ["J18.9"]
    checked = {r["id"] for r in deps.opa.inputs[-1]["resources"]}
    assert "ov-jd-cond-mdd" in checked                    # policy saw the labelled condition


@pytest.mark.anyio
async def test_policy_hears_what_the_order_cites_not_what_the_agent_attached(env) -> None:
    """Over-sharing: the agent attaches Jane's prediabetes and lung cancer to the oxygen
    request. The gateway reads the oxygen order from the EHR and tells policy which attached
    records it cites; the rest is for policy to withhold (minimum necessary)."""
    server, deps = env
    await call(server, "request_prior_authorization", patient_id="jane-doe",
               service="Home oxygen", diagnosis_codes=["J18.9", "R09.02"],
               evidence_ids=["ov-jd-obs-spo2-walk", "ov-jd-cond-hypox",
                             "1db55074-e61f-4723-a719-490059069c80",
                             "94d290a4-ed55-4711-bcf7-c2bb4fa39405"])
    asked = deps.opa.inputs[-1]
    assert asked["order"] == {"evidence": ["ov-jd-obs-spo2-walk"],
                              "diagnosis_codes": ["J18.9", "R09.02"]}
    cited = {r["id"]: r["cited"] for r in asked["resources"]}
    assert cited["ov-jd-obs-spo2-walk"] and cited["ov-jd-cond-hypox"]   # by id, by code
    assert not cited["1db55074-e61f-4723-a719-490059069c80"]             # prediabetes
    assert not cited["94d290a4-ed55-4711-bcf7-c2bb4fa39405"]             # lung cancer


@pytest.mark.anyio
async def test_other_disclosures_have_no_order(env) -> None:
    server, deps = env
    await call(server, "check_eligibility", patient_id="jane-doe")
    assert deps.opa.inputs[-1]["order"] is None


@pytest.mark.anyio
async def test_policy_hears_the_agents_task_binding(env) -> None:
    """Task binding holds at the disclosure gateway too: policy is told whose data this is
    and which patient the agent's task is about (from the EHR gateway, not the agent)."""
    server, deps = env
    asked = []
    deps.assignment_source = lambda agent, user: asked.append((agent, user)) or ["jane-doe"]
    await call(server, "check_eligibility", patient_id="pt-87c46349")
    assert asked == [("benefits-agent", "dr.morgana")]
    sent = deps.opa.inputs[-1]
    assert sent["patient_id"] == "pt-87c46349"
    assert sent["assignment"] == {"bound": True, "patients": ["jane-doe"]}
