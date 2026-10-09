"""The policy questions ehr-mcp asks OPA (see infra/opa/policies/hospital/ehr.rego).

Every answer fails closed: if OPA is unreachable, no tools are listed, every call is denied,
every labelled record is withheld, and every refill needs a physician.
"""

from __future__ import annotations

from typing import Any

from hacknc.common.mcp_server import Caller
from hacknc.common.policy import OpaClient, PolicyDecision, subject_input
from hacknc.ehr.records import Resource, security_labels

POLICY = "hospital/ehr"


class EhrGate:
    def __init__(self, opa: OpaClient) -> None:
        self._opa = opa

    def tool_decision(self, caller: Caller, tool: str, patient_id: str | None,
                      assigned: list[str] | None = None,
                      linked_patient: str | None = None) -> PolicyDecision:
        """``assigned``: the patients this agent's current task is about (None: not bound,
        e.g. a person using the hospital app). ``linked_patient``: for a patient, the record
        their portal account is linked to."""
        return self._opa.decide(f"{POLICY}/tool_decision", {
            "tool": tool,
            "subject": {**subject_input(caller.claims), "linked_patient": linked_patient},
            "patient_id": patient_id,
            "resource": f"Patient/{patient_id}" if patient_id else None,
            "assignment": {"bound": assigned is not None, "patients": assigned or []},
        })

    def visible_tools(self, caller: Caller) -> list[str]:
        result = self._opa.query(f"{POLICY}/visible_tools",
                                 {"subject": subject_input(caller.claims)})
        return list(result) if isinstance(result, list) else []

    def filter(self, caller: Caller, resources: list[Resource],
               break_glass: bool = False, own_record: bool = False
               ) -> tuple[list[Resource], list[dict[str, Any]]]:
        """Split resources into (visible, withheld-with-reasons). ``own_record``: a patient
        looking at their own chart (patients may see their own sensitive records)."""
        labelled = [{"id": r["id"], "type": r["resourceType"], "labels": security_labels(r)}
                    for r in resources]
        result = self._opa.query(f"{POLICY}/withheld", {
            "subject": subject_input(caller.claims), "resources": labelled,
            "break_glass": break_glass, "own_record": own_record})
        if not isinstance(result, list):  # OPA down: hide anything labelled
            result = [{"id": r["id"], "type": r["type"], "reason": "policy engine unavailable"}
                      for r in labelled if r["labels"]]
        hidden = {w["id"] for w in result}
        return [r for r in resources if r["id"] not in hidden], result

