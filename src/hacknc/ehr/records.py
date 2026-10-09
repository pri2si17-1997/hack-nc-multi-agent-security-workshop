"""Load curated Synthea patients plus the workshop overlay, and query them.

Data layout::

    data/curated/<patient-id>.json   trimmed Synthea record (see tools/curate_synthea.py)
    data/overlay/<patient-id>.json   hand-written additions for the discharge story: admission,
                                     hospital meds, allergies, labs, notes, portal messages,
                                     sensitive labels, pharmacy / family doctor / consent
    data/overlay/generated/<id>.json the current stay of every other patient, same shape
                                     (``make admissions``, tools/generate_admissions.py)

Every admission carries a ``dischargePlan`` with one schema for all patients::

    {"readyDate": "2026-10-03",
     "medications": [{"action": "new|renew|resume|stop", "drug", "dose", "quantity", "sig",
                      "reason"}],
     "equipment":   [{"item", "detail", "prior_auth", "evidence": [observation ids],
                      "diagnosis_codes"}],
     "followUp":    {"specialty", "within_days", "reason", "note"},
     "warningSigns": "..."}

It is the physician's intent as written; medication reconciliation verifies it.

Patient portal (runtime state, forgotten on the demo reset): a patient's portal account is
linked to their record (``preferences.portalAccount``). Messages they send are added to the
chart; requests ("please raise my dose") wait for a physician, and an accepted medication
request is added to the discharge plan, where reconciliation and signatures still apply.

Records are FHIR-shaped dicts. Sensitivity is expressed the FHIR way, as security labels in
``meta.security`` (HL7 ActCode ``PSY``, ``HIV``...); ``security_labels`` reads them.
"""

from __future__ import annotations

import copy
import itertools
import json
import threading
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

Resource = dict[str, Any]
SECTIONS = ("conditions", "medications", "observations", "claims", "notes", "allergies",
            "encounters", "messages")


class PatientNotFound(KeyError):
    """No patient with that id."""


def security_labels(resource: Resource) -> list[str]:
    """Confidentiality labels on a resource, e.g. ``["PSY"]``."""
    return sorted({s.get("code", "") for s in resource.get("meta", {}).get("security", [])} - {""})


def tags(resource: Resource) -> set[str]:
    return {t.get("code", "") for t in resource.get("meta", {}).get("tag", [])}


@dataclass
class PatientRecord:
    patient: Resource
    conditions: list[Resource] = field(default_factory=list)
    medications: list[Resource] = field(default_factory=list)
    observations: list[Resource] = field(default_factory=list)
    claims: list[Resource] = field(default_factory=list)
    notes: list[Resource] = field(default_factory=list)
    allergies: list[Resource] = field(default_factory=list)
    encounters: list[Resource] = field(default_factory=list)
    messages: list[Resource] = field(default_factory=list)
    coverage: Resource | None = None
    preferences: dict[str, Any] = field(default_factory=dict)
    posted_instructions: list[Resource] = field(default_factory=list)
    discharge: Resource | None = None   # set when the current stay ends (runtime state)
    requests: list[dict[str, Any]] = field(default_factory=list)   # from the patient portal

    @property
    def id(self) -> str:
        return self.patient["id"]

    @property
    def display_name(self) -> str:
        name = self.patient["name"][0]
        return " ".join([*name.get("given", []), name.get("family", "")]).strip()

    @property
    def age(self) -> int:
        born = date.fromisoformat(self.patient["birthDate"])
        today = date.today()
        return today.year - born.year - ((today.month, today.day) < (born.month, born.day))

    @property
    def mrn(self) -> str:
        return next((i["value"] for i in self.patient.get("identifier", [])
                     if i["type"] == "MRN"), "")

    def admission(self) -> Resource | None:
        """The current inpatient stay, if any."""
        return next((e for e in self.encounters
                     if e.get("class") == "inpatient" and e.get("status") == "in-progress"), None)

    @property
    def portal_username(self) -> str:
        """The username this patient's portal account gets: first.last, letters only."""
        name = self.patient["name"][0]
        words = [*name.get("given", [])[:1], name.get("family", "")]
        return ".".join("".join(c for c in w.lower() if c.isalpha()) for w in words if w)

    @property
    def portal_account(self) -> str | None:
        return self.preferences.get("portalAccount")

    def open_requests(self) -> list[dict[str, Any]]:
        return [r for r in self.requests if r["status"] == "open"]

    @property
    def bed(self) -> int:
        """Bed number of the current stay (999 if not admitted), for ward ordering."""
        location = (self.admission() or {}).get("location", "")
        digits = location.rsplit(" ", 1)[-1]
        return int(digits) if digits.isdigit() else 999

    def demographics(self) -> dict[str, Any]:
        """Header facts that are never sensitive (no SSN: nobody in the demo needs it)."""
        stay = self.admission()
        return {"id": self.id, "name": self.display_name, "gender": self.patient.get("gender"),
                "birthDate": self.patient.get("birthDate"), "age": self.age,
                "mrn": self.mrn[:8],
                "admission": {"reason": stay["reason"], "since": stay["period"]["start"][:10],
                              "location": stay.get("location"),
                              "attending": stay.get("attending")} if stay else None,
                "discharged": self.discharge, "open_requests": len(self.open_requests())}

    def active_conditions(self) -> list[Resource]:
        return [c for c in self.conditions if c.get("clinicalStatus") == "active"]

    def current_medications(self) -> list[Resource]:
        """Everything she is on or was on before admission: active, held and hospital meds."""
        return [m for m in self.medications if m.get("status") in ("active", "on-hold")]


class EhrRepository:
    """All patients, in memory. Posted patient instructions are added at runtime."""

    def __init__(self, records: dict[str, PatientRecord]) -> None:
        self._records = records
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        # What the portal may change at runtime, as loaded (restored on reset).
        self._original = {pid: copy.deepcopy((r.preferences, r.encounters, r.messages))
                          for pid, r in records.items()}

    @classmethod
    def load(cls, curated_dir: Path, overlay_dir: Path | None = None,
             generated: bool = True) -> EhrRepository:
        """Curated records, then hand-written overlays, then generated admissions."""
        records: dict[str, PatientRecord] = {}
        for path in sorted(Path(curated_dir).glob("*.json")):
            raw = json.loads(path.read_text())
            record = PatientRecord(patient=raw["patient"], coverage=raw.get("coverage"),
                                   **{s: raw.get(s, []) for s in SECTIONS})
            records[record.id] = record
        if overlay_dir:
            paths = sorted(Path(overlay_dir).glob("*.json"))
            if generated:
                paths += sorted((Path(overlay_dir) / "generated").glob("*.json"))
            for path in paths:
                if path.stem in records:
                    _apply_overlay(records[path.stem], json.loads(path.read_text()))
        for record in records.values():
            for med in record.medications:
                med.setdefault("category", "home")  # Synthea meds are outpatient prescriptions
        return cls(records)

    def __len__(self) -> int:
        return len(self._records)

    def get(self, patient_id: str) -> PatientRecord:
        try:
            return self._records[patient_id]
        except KeyError:
            raise PatientNotFound(patient_id) from None

    def search(self, query: str = "") -> list[PatientRecord]:
        """Case-insensitive match on name, id or MRN prefix. Empty query lists everyone.
        Admitted patients first, in bed order, then by name."""
        q = query.strip().lower()
        matches = [r for r in self._records.values()
                   if not q or q in r.display_name.lower() or r.id.startswith(q)
                   or r.mrn.lower().startswith(q)]
        return sorted(matches, key=lambda r: (r.admission() is None, r.bed, r.display_name))

    def post_instructions(self, patient_id: str, text: str, author: str) -> Resource:
        """Publish discharge instructions to the patient portal."""
        posted = {"resourceType": "Communication", "id": f"instr-{patient_id}",
                  "sent": datetime.now().isoformat(timespec="seconds"),
                  "sender": f"{author} (AI draft for the care team)", "text": text}
        with self._lock:
            self.get(patient_id).posted_instructions = [posted]
        return posted

    def record_discharge(self, patient_id: str, ordered_by: str, recorded_by: str,
                         disposition: str, note: str = "") -> Resource:
        """End the current stay. Raises ``ValueError`` if there is none or it already ended."""
        with self._lock:
            record = self.get(patient_id)
            if record.admission() is None:
                raise ValueError(f"{record.display_name} has no current hospital stay")
            if record.discharge is not None:
                raise ValueError(f"{record.display_name} was already discharged "
                                 f"{record.discharge['at']}")
            record.discharge = {"at": datetime.now().isoformat(timespec="seconds"),
                                "disposition": disposition, "ordered_by": ordered_by,
                                "recorded_by": recorded_by, "note": note}
            return record.discharge

    def reset_runtime_state(self) -> None:
        with self._lock:
            for pid, record in self._records.items():
                record.posted_instructions = []
                record.discharge = None
                record.requests = []
                prefs, encounters, messages = copy.deepcopy(self._original[pid])
                record.preferences, record.encounters, record.messages = \
                    prefs, encounters, messages

    # ------------------------------------------------------------------ patient portal

    def patient_for_account(self, username: str) -> str | None:
        """The record a portal account is linked to (None: not a patient account)."""
        return next((r.id for r in self._records.values() if r.portal_account == username),
                    None)

    def post_portal_message(self, patient_id: str, text: str, sender: str,
                            flags: list[str] | None = None) -> Resource:
        """A message from the patient, now part of the chart (untrusted text)."""
        with self._lock:
            record = self.get(patient_id)
            tags = [{"code": "patient-portal"}]
            if "prompt_injection" in (flags or []):
                tags.append({"code": "contains-prompt-injection"})
            message = {"resourceType": "Communication", "id": f"msg-portal-{next(self._ids)}",
                       "sent": datetime.now().isoformat(timespec="seconds"),
                       "sender": f"{record.display_name} (patient portal, {sender})",
                       "text": text,
                       "meta": {"tag": tags}}
            record.messages.append(message)
            return message

    def submit_request(self, patient_id: str, kind: str, summary: str, by: str,
                       drug: str | None = None, requested_dose: str | None = None,
                       flags: list[str] | None = None,
                       message_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            record = self.get(patient_id)
            request = {"id": f"req-{next(self._ids)}", "kind": kind, "summary": summary,
                       "drug": drug, "requested_dose": requested_dose, "flags": flags or [],
                       "message_id": message_id, "status": "open", "submitted_by": by,
                       "submitted_at": datetime.now().isoformat(timespec="seconds")}
            record.requests.append(request)
            return request

    def decide_request(self, patient_id: str, request_id: str, accept: bool, by: str,
                       note: str = "") -> dict[str, Any]:
        """A physician's decision. An accepted medication request becomes a discharge-plan
        item (replacing the plan's item for the same drug). Raises ``ValueError``."""
        with self._lock:
            record = self.get(patient_id)
            request = next((r for r in record.requests if r["id"] == request_id), None)
            if request is None:
                raise ValueError(f"no request {request_id}")
            if request["status"] != "open":
                raise ValueError(f"request {request_id} was already {request['status']}")
            request.update(status="accepted" if accept else "declined", decided_by=by,
                           decided_at=datetime.now().isoformat(timespec="seconds"), note=note)
            if accept and request["kind"] in ("medication_change", "refill") and request["drug"]:
                request["plan_item"] = self._add_to_plan(record, request, by)
            return request

    def _add_to_plan(self, record: PatientRecord, request: dict[str, Any],
                     by: str) -> dict[str, Any] | None:
        stay = record.admission()
        if stay is None:
            return None
        drug = request["drug"].lower()
        home = next((m for m in record.current_medications()
                     if m.get("category") == "home"
                     and drug in m["medication"]["text"].lower()), None)
        plan = stay.setdefault("dischargePlan", {}).setdefault("medications", [])
        current = next((m for m in plan if m["drug"].lower() == drug), None)
        dose = request.get("requested_dose") or (current or home or {}).get("dose")
        if not dose:   # "raise my insulin": no number anywhere. The physician sets the dose.
            return None
        item = {"action": "renew" if home else "new", "drug": drug,
                "dose": dose,
                "quantity": (current or home or {}).get("quantity", 30),
                "sig": (current or home or {}).get("sig", "as directed"),
                "reason": f"patient request {request['id']}, accepted by {by}"}
        plan[:] = [m for m in plan if m["drug"].lower() != drug] + [item]
        return item

    def set_consent(self, patient_id: str, share_sensitive_with_pcp: bool,
                    by: str) -> dict[str, Any]:
        with self._lock:
            record = self.get(patient_id)
            consent = {**record.preferences.get("consent", {}),
                       "shareSensitiveWithPcp": share_sensitive_with_pcp,
                       "note": ("Patient consented in the portal to share sensitive records "
                                "with their family doctor" if share_sensitive_with_pcp else
                                "Patient declined in the portal to share sensitive records "
                                "outside the hospital"),
                       "changed_by": by, "changed_at": datetime.now().isoformat(timespec="seconds")}
            record.preferences = {**record.preferences, "consent": consent}
            return consent


def _apply_overlay(record: PatientRecord, overlay: dict[str, Any]) -> None:
    for section in SECTIONS:
        getattr(record, section)[:0] = overlay.get(section, [])  # overlay first: most recent
    record.notes.sort(key=lambda n: n.get("date", ""), reverse=True)
    record.messages.sort(key=lambda m: m.get("sent", ""))
    if overlay.get("coverage"):
        record.coverage = overlay["coverage"]
    if overlay.get("preferences"):
        record.preferences = overlay["preferences"]
    for hold in overlay.get("medicationHolds", []):
        for med in record.medications:
            if med.get("status") == "active" and hold["match"] in med["medication"]["text"].lower():
                med["status"] = "on-hold"
                med["holdReason"] = hold["reason"]
                if hold.get("resumeIf"):
                    med["resumeIf"] = hold["resumeIf"]
