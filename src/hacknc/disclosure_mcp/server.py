"""disclosure-mcp: an MCP server that governs every disclosure to an outside organization.

Every tool follows the same path, implemented once in ``_disclose``:

1. resolve the patient's identity fields and any evidence records *from the EHR itself*
   (agents name records by id; they cannot relabel or rewrite them), and for a prior
   authorization, what the physician's order cites
2. ask OPA (``hospital/disclosure/decision``) what may go, to whom, for what purpose
3. strip every field and record that may not go, and say why
4. exchange the caller's token for one addressed to that partner only, and send
5. record the disclosure in the audit trail (the patient's accounting of disclosures)

Signed content (prescriptions, the discharge summary) is checked against the physician's
signature first: if an agent changed it after signing, nothing is sent.
"""

from __future__ import annotations

import contextvars
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from opentelemetry import trace

from hacknc.approval_service.client import ApprovalApi
from hacknc.common.audit import AuditClient, AuditEvent, actor_fields
from hacknc.common.auth import AuthError, KeycloakClient
from hacknc.common.flags import FlagReader
from hacknc.common.mcp_server import Caller, current_caller
from hacknc.common.policy import OpaClient, subject_input
from hacknc.ehr.records import EhrRepository, PatientNotFound, Resource, security_labels
from hacknc.ehr_mcp.assignments import is_bound
from hacknc.partners.app import PARTNER_NAMES
from hacknc.partners.client import ENDPOINTS, PartnerApi

SERVER_NAME = "disclosure-mcp"
POLICY = "hospital/disclosure/decision"
tracer = trace.get_tracer(__name__)

INSTRUCTIONS = ("Disclosure gateway: the only way to send patient data outside the hospital. "
                "Every disclosure is checked for minimum necessary data and recorded.")


@dataclass
class DisclosureDeps:
    repo: EhrRepository
    opa: OpaClient
    keycloak: KeycloakClient
    secrets: Callable[[str], str]
    partners: PartnerApi
    approvals: ApprovalApi
    audit: AuditClient | None = None
    flags: FlagReader | None = None
    caller_source: Callable[[], Caller | None] = current_caller
    # The patient's *current* consent (they can change it in the portal). Must fail closed:
    # return {} (no consent) when it can't be read.
    consent_source: Callable[[str], dict[str, Any]] | None = None
    # (agent, user) -> the patients that agent's current task is about, as the coordinator
    # told the EHR gateway. Must fail closed: return [] when it can't be read.
    assignment_source: Callable[[str, str], list[str]] | None = None

    def assignment(self, caller: Caller) -> dict[str, Any]:
        """Task binding, as the EHR gateway applies it: agents are bound, people are not."""
        if not is_bound(caller.agent) or self.assignment_source is None:
            return {"bound": False, "patients": []}
        return {"bound": True, "patients": self.assignment_source(caller.agent, caller.user)}

    def consent(self, record: Any) -> dict[str, Any]:
        if self.consent_source is None:
            return record.preferences.get("consent", {})
        return self.consent_source(record.id)


@dataclass
class Outcome:
    """What one disclosure did; returned to the agent and recorded."""

    destination: str
    purpose: str
    sent_fields: list[str] = field(default_factory=list)
    stripped: list[dict[str, Any]] = field(default_factory=list)
    response: dict[str, Any] = field(default_factory=dict)


_caller: contextvars.ContextVar[Caller] = contextvars.ContextVar("disclosure_caller")


def describe(resource: Resource) -> str:
    """A record as one line of evidence text."""
    kind = resource.get("resourceType")
    if kind == "Observation":
        when = (resource.get("effectiveDateTime") or "")[:10]
        return f"{resource['code']['text']}: {resource.get('value')} ({when})"
    if kind == "Condition":
        code = (resource["code"].get("coding") or [{}])[0].get("code", "")
        return f"{resource['code']['text']}" + (f" ({code})" if code else "")
    if kind == "DocumentReference":
        return f"{resource['type']['text']} ({resource.get('date')}): {resource.get('text', '')}"
    return str(resource.get("id"))


class DisclosureMcpServer(MCPServer):
    def __init__(self, deps: DisclosureDeps, **kwargs: Any) -> None:
        super().__init__(name=SERVER_NAME, instructions=INSTRUCTIONS, **kwargs)
        self.deps = deps
        _register_tools(self, deps)

    async def call_tool(self, name: str, arguments: dict[str, Any], context=None):
        caller = self.deps.caller_source()
        if caller is None:
            raise ToolError("authentication required")
        token = _caller.set(caller)
        try:
            return await super().call_tool(name, arguments, context)
        finally:
            _caller.reset(token)


# --------------------------------------------------------------------------- core


def _record(deps: DisclosureDeps, patient_id: str) -> Any:
    try:
        return deps.repo.get(patient_id)
    except PatientNotFound:
        raise ToolError(f"no patient with id '{patient_id}'") from None


def _exchange(deps: DisclosureDeps, caller: Caller, audience: str,
              scopes: list[str] | None = None) -> str:
    return deps.keycloak.exchange_token(SERVER_NAME, deps.secrets(SERVER_NAME), caller.token,
                                        audience, scopes or []).access_token


def _disclose(deps: DisclosureDeps, purpose: str, patient_id: str,
              candidate: dict[str, Any], evidence: list[Resource] | None = None,
              coded: dict[str, Resource] | None = None,
              order: dict[str, list[str]] | None = None) -> Outcome:
    """Check, strip, send and record one disclosure. Raises ToolError if it may not go.

    ``coded``: diagnosis code -> the patient's condition with that code. Those conditions are
    checked by policy like evidence (a code is as sensitive as its record), and the codes of
    any that may not go are removed. ``order``: what the physician's order cites (record ids
    and diagnosis codes); policy may let only those records go."""
    caller = _caller.get()
    record = _record(deps, patient_id)
    destination = ENDPOINTS[purpose][0]
    evidence = evidence or []
    coded = coded or {}
    checked = evidence + [c for c in coded.values() if c not in evidence]
    partner_enabled = deps.flags.on(f"partner.{destination}") if deps.flags else True

    with tracer.start_as_current_span(f"disclosure-mcp {purpose}") as span:
        span.set_attributes({
            "mcp.server": SERVER_NAME, "agent.id": caller.agent, "user.name": caller.user,
            "patient.id": patient_id, "disclosure.purpose": purpose,
            "disclosure.destination": destination,
            "disclosure.requested_fields": ",".join(sorted(candidate)),
        })
        decision = deps.opa.query(POLICY, {
            "purpose": purpose, "destination": destination, "patient_id": record.id,
            "assignment": deps.assignment(caller),
            "subject": subject_input(caller.claims), "fields": sorted(candidate),
            "resources": [{"id": r["id"], "type": r["resourceType"],
                           "labels": security_labels(r), "cited": _cited(r, order)}
                          for r in checked],
            "order": order,
            "consent": deps.consent(record),
            "partner_enabled": partner_enabled,
        }) or {"allow": False, "reason": "policy engine unavailable"}
        stripped = list(decision.get("stripped_fields", [])) + list(
            decision.get("stripped_resources", []))
        span.set_attributes({
            "policy.decision": "allow" if decision["allow"] else "deny",
            "policy.reason": decision["reason"],
            "disclosure.stripped": len(stripped),
            "disclosure.stripped_reasons": "; ".join(sorted({s["reason"] for s in stripped})),
        })
        if not decision["allow"]:
            _audit(deps, caller, record, purpose, destination, "deny", decision["reason"],
                   severity="warning")
            raise ToolError(f"DENIED by disclosure policy: {decision['reason']}")

        hidden = {s["id"] for s in decision.get("stripped_resources", [])}
        payload = {f: candidate[f] for f in decision["allowed_fields"]}
        if "evidence" in payload:
            payload["evidence"] = [describe(r) for r in evidence if r["id"] not in hidden]
        if "diagnosis_codes" in payload:
            payload["diagnosis_codes"] = [c for c in payload["diagnosis_codes"]
                                          if (coded.get(c) or {}).get("id") not in hidden]
        if "summary" in payload:  # drop sections drawn from records that may not leave
            sections = payload["summary"].get("sections", [])
            kept = [x for x in sections if x.get("source") not in hidden]
            payload["summary"] = {**payload["summary"], "sections": kept,
                                  "withheld_sections": len(sections) - len(kept)}
        span.set_attribute("disclosure.sent_fields", ",".join(sorted(payload)))

        try:
            token = _exchange(deps, caller, destination)
        except AuthError as exc:
            raise ToolError(f"Keycloak refused a token for {destination}: {exc}") from exc
        result = deps.partners.send(purpose, token, payload)
        span.set_attribute("partner.status", result.status)
        if not result.ok:
            _audit(deps, caller, record, purpose, destination, "deny",
                   f"{PARTNER_NAMES[destination]} refused: {result.reason}", severity="warning")
            raise ToolError(f"{PARTNER_NAMES[destination]} refused: {result.reason}")

        outcome = Outcome(destination, purpose, sorted(payload), stripped, result.body)
        reason = (f"sent {', '.join(outcome.sent_fields)} to {PARTNER_NAMES[destination]} "
                  f"for {purpose.replace('_', ' ')}")
        if stripped:
            reason += f"; withheld {len(stripped)} item(s)"
        _audit(deps, caller, record, purpose, destination, "allow", reason,
               severity="warning" if stripped else "info", outcome=outcome)
        return outcome


def _cited(resource: Resource, order: dict[str, list[str]] | None) -> bool:
    """Does the physician's order cite this record, by id or by one of its diagnosis codes?"""
    if not order:
        return False
    codes = {c.get("code") for c in (resource.get("code") or {}).get("coding", [])}
    return resource["id"] in order["evidence"] or bool(codes & set(order["diagnosis_codes"]))


def _prior_auth_order(record: Any) -> dict[str, list[str]]:
    """What the current stay's equipment orders that need prior authorization cite, read from
    the EHR (never from the agent): the evidence record ids and the diagnosis codes."""
    plan = (record.admission() or {}).get("dischargePlan") or {}
    items = [e for e in plan.get("equipment", []) if e.get("prior_auth")]
    return {"evidence": sorted({i for e in items for i in e.get("evidence", [])}),
            "diagnosis_codes": sorted({c for e in items for c in e.get("diagnosis_codes", [])})}


def _audit(deps: DisclosureDeps, caller: Caller, record: Any, purpose: str, destination: str,
           decision: str, reason: str, severity: str = "info",
           outcome: Outcome | None = None) -> None:
    if deps.audit is None:
        return
    deps.audit.record(AuditEvent(
        action="disclosure.sent" if decision == "allow" else "disclosure.blocked",
        decision=decision, severity=severity, **actor_fields(caller.claims),
        agent_chain=[caller.agent, SERVER_NAME, destination],
        resource=f"Patient/{record.id}", patient_id=record.id, reason=reason,
        policy=POLICY,
        details={"destination": destination, "destination_name": PARTNER_NAMES[destination],
                 "purpose": purpose,
                 "fields": outcome.sent_fields if outcome else [],
                 "withheld": [s.get("field") or s.get("type") for s in outcome.stripped]
                 if outcome else [],
                 "scopes": sorted(caller.claims.scopes)}))


def _identity(record: Any) -> dict[str, Any]:
    return {"name": record.display_name, "dob": record.patient.get("birthDate")}


# What a signature must have been given for, per purpose (approval request ``kind``).
SIGNED_KINDS = {"prescription": "prescription", "care_transition": "discharge_summary"}


def _signed(deps: DisclosureDeps, signature_id: str, content: dict[str, Any], record: Any,
            purpose: str) -> dict[str, Any]:
    """Check ``content`` against the physician's signature and use the signature up: the
    same content, for this patient, as this kind of document. A refusal is recorded here, by
    the gateway: nothing clinical leaves without a matching signature."""
    caller = _caller.get()
    token = _exchange(deps, caller, "approval-service", ["approval:request"])
    result = deps.approvals.consume(token, signature_id, content, record.id,
                                    SIGNED_KINDS[purpose])
    if not result.ok:
        _audit(deps, caller, record, purpose, ENDPOINTS[purpose][0], "deny",
               f"no valid physician signature for this content: {result.reason}",
               severity="warning")
        raise ToolError(f"DENIED: {result.reason}")
    return result.body


def _outcome_view(outcome: Outcome) -> dict[str, Any]:
    return {"destination": PARTNER_NAMES[outcome.destination], "sent": outcome.sent_fields,
            "withheld": outcome.stripped, "response": outcome.response}


# --------------------------------------------------------------------------- tools


def _register_tools(server: MCPServer, deps: DisclosureDeps) -> None:
    sends = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)

    @server.tool(annotations=sends)
    def check_eligibility(patient_id: str,
                          extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """Ask the patient's payer whether her coverage is active. Anything in ``extra`` the
        purpose doesn't need is withheld by policy (minimum necessary)."""
        record = _record(deps, patient_id)
        member = (record.coverage or {}).get("memberId")
        return _outcome_view(_disclose(deps, "eligibility", patient_id,
                                       {**(extra or {}), **_identity(record),
                                        "member_id": member}))

    @server.tool(annotations=sends)
    def request_prior_authorization(patient_id: str, service: str, diagnosis_codes: list[str],
                                    evidence_ids: list[str],
                                    extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """Ask the payer to authorize a service (e.g. home oxygen). Evidence is named by record
        id; the gateway attaches the records itself and strips anything not allowed."""
        record = _record(deps, patient_id)
        everything = {r["id"]: r for r in record.observations + record.conditions + record.notes}
        evidence = [everything[i] for i in evidence_ids if i in everything]
        candidate = {**_identity(record), "member_id": (record.coverage or {}).get("memberId"),
                     "service": service, "diagnosis_codes": diagnosis_codes,
                     "evidence": evidence_ids, **(extra or {})}
        coded = {coding["code"]: c for c in record.conditions
                 for coding in c["code"].get("coding", []) if coding.get("code") in diagnosis_codes}
        return _outcome_view(_disclose(deps, "prior_authorization", patient_id, candidate,
                                       evidence, coded, _prior_auth_order(record)))

    @server.tool(annotations=sends)
    def query_controlled_substance_registry(patient_id: str, drug: str) -> dict[str, Any]:
        """Check the state registry (PDMP) before prescribing a controlled substance."""
        record = _record(deps, patient_id)
        return _outcome_view(_disclose(deps, "registry_query", patient_id,
                                       {**_identity(record), "drug": drug}))

    @server.tool(annotations=sends)
    def send_prescription(signature_id: str, patient_id: str,
                          prescription: dict[str, Any]) -> dict[str, Any]:
        """Send a physician-signed prescription to the patient's pharmacy."""
        record = _record(deps, patient_id)
        signed = _signed(deps, signature_id, prescription, record, "prescription")
        signature = {"signer": signed.get("decided_by"), "fingerprint": signed["fingerprint"],
                     "signed_at": signed.get("decided_at")}
        return _outcome_view(_disclose(deps, "prescription", patient_id, {
            **_identity(record), "prescription": prescription, "signature": signature}))

    @server.tool(annotations=sends)
    def book_follow_up(patient_id: str, reason: str, within_days: int = 7,
                       specialty: str = "primary care") -> dict[str, Any]:
        """Book a follow-up visit (primary care or a specialist clinic of the same group)."""
        record = _record(deps, patient_id)
        return _outcome_view(_disclose(deps, "appointment", patient_id, {
            **_identity(record), "reason": reason, "within_days": within_days,
            "specialty": specialty}))

    @server.tool(annotations=sends)
    def send_discharge_summary(signature_id: str, patient_id: str,
                               summary: dict[str, Any]) -> dict[str, Any]:
        """Send the physician-signed discharge summary to the family doctor. Sections from
        sensitive records are withheld unless the patient consented."""
        record = _record(deps, patient_id)
        _signed(deps, signature_id, summary, record, "care_transition")
        sections = summary.get("sections", [])
        sources = {r["id"]: r for r in record.conditions + record.notes + record.medications}
        evidence = [sources[s["source"]] for s in sections if s.get("source") in sources]
        outcome = _disclose(deps, "care_transition", patient_id, {
            **_identity(record), "document_type": "Discharge summary", "summary": summary},
            evidence)
        return _outcome_view(outcome)
