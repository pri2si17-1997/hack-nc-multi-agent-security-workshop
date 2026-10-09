"""Pick a handful of Synthea patients and trim them to what the demo needs.

Synthea (https://github.com/synthetichealth/synthea) generates fully synthetic patients as
FHIR R4 bundles: about 1 MB each, mostly encounters and billing detail. This script keeps,
per patient, only:

* Patient            name (digits Synthea appends are removed), gender, birth date, city,
                     medical record number and the synthetic SSN (999-xx-xxxx)
* Condition          active and resolved diagnoses
* MedicationRequest  latest request per medication
* Observation        latest value per vital sign / lab
* Claim              last five claims (with the insurer)

Output: ``data/curated/<patient-id>.json``, one small FHIR-shaped file per patient. The demo
patient is renamed "Jane Doe" with the stable id ``jane-doe``; hand-written workshop records
are layered on top from ``data/overlay/`` when the EHR server loads.

    make curate                       # reads data/synthea/raw/fhir/*.json
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
RAW_DIR = ROOT / "data" / "synthea" / "raw" / "fhir"
OUT_DIR = ROOT / "data" / "curated"

DEMO_PATIENT_ID = "jane-doe"
REFERENCE_YEAR = 2026
MAX_OBSERVATIONS = 14
MAX_CLAIMS = 5
KEPT_IDENTIFIERS = {"Medical Record Number": "MRN", "Social Security Number": "SSN"}
SYSTOLIC_LOINC = "8480-6"


# --------------------------------------------------------------------------- trimming helpers


def clean_name(text: str) -> str:
    """Synthea appends digits to names ("Daniella68"); drop them."""
    return re.sub(r"\d+", "", text).strip()


def coding(concept: dict[str, Any] | None) -> dict[str, Any]:
    concept = concept or {}
    first = (concept.get("coding") or [{}])[0]
    return {"text": concept.get("text") or first.get("display", ""),
            "coding": [{k: first[k] for k in ("system", "code", "display") if k in first}]}


def trim_patient(resource: dict[str, Any], new_id: str) -> dict[str, Any]:
    official = next((n for n in resource.get("name", []) if n.get("use") == "official"),
                    (resource.get("name") or [{}])[0])
    identifiers = [
        {"type": KEPT_IDENTIFIERS[i["type"]["text"]], "value": i["value"]}
        for i in resource.get("identifier", [])
        if i.get("type", {}).get("text") in KEPT_IDENTIFIERS
    ]
    address = (resource.get("address") or [{}])[0]
    return {
        "resourceType": "Patient",
        "id": new_id,
        "identifier": identifiers,
        "name": [{"family": clean_name(official.get("family", "")),
                  "given": [clean_name(g) for g in official.get("given", [])]}],
        "gender": resource.get("gender"),
        "birthDate": resource.get("birthDate"),
        "address": [{"city": address.get("city"), "state": address.get("state")}],
    }


def trim_condition(resource: dict[str, Any]) -> dict[str, Any]:
    status = resource.get("clinicalStatus", {}).get("coding", [{}])[0].get("code", "unknown")
    return {"resourceType": "Condition", "id": resource["id"], "clinicalStatus": status,
            "code": coding(resource.get("code")), "onsetDateTime": resource.get("onsetDateTime"),
            "abatementDateTime": resource.get("abatementDateTime")}


def trim_medication(resource: dict[str, Any]) -> dict[str, Any]:
    return {"resourceType": "MedicationRequest", "id": resource["id"],
            "status": resource.get("status"),
            "medication": coding(resource.get("medicationCodeableConcept")),
            "authoredOn": resource.get("authoredOn")}


def trim_observation(resource: dict[str, Any]) -> dict[str, Any]:
    value: Any = None
    if "valueQuantity" in resource:
        q = resource["valueQuantity"]
        value = f"{round(q.get('value', 0), 1)} {q.get('unit', '')}".strip()
    elif "component" in resource:  # blood pressure: show systolic/diastolic, in that order
        components = sorted(resource["component"],
                            key=lambda c: c.get("code", {}).get("coding", [{}])[0].get("code")
                            != SYSTOLIC_LOINC)
        parts = [round(c.get("valueQuantity", {}).get("value", 0)) for c in components]
        unit = components[0].get("valueQuantity", {}).get("unit", "")
        value = f"{'/'.join(str(p) for p in parts)} {unit}".strip()
    elif "valueCodeableConcept" in resource:
        value = resource["valueCodeableConcept"].get("text")
    category = resource.get("category", [{}])[0].get("coding", [{}])[0].get("code")
    return {"resourceType": "Observation", "id": resource["id"], "category": category,
            "code": coding(resource.get("code")), "value": value,
            "effectiveDateTime": resource.get("effectiveDateTime")}


def trim_claim(resource: dict[str, Any]) -> dict[str, Any]:
    return {"resourceType": "Claim", "id": resource["id"], "status": resource.get("status"),
            "type": resource.get("type", {}).get("coding", [{}])[0].get("code"),
            "created": resource.get("created"),
            "insurer": resource.get("insurance", [{}])[0].get("coverage", {}).get("display"),
            "total": resource.get("total", {}).get("value")}


def coverage_from_claims(patient_id: str, claims: list[dict[str, Any]]) -> dict[str, Any]:
    """Synthea has no Coverage resource; derive one from the insurer on the latest claim."""
    insurer = claims[0]["insurer"] if claims else None
    insured = bool(insurer) and insurer != "NO_INSURANCE"
    return {"resourceType": "Coverage", "id": f"cov-{patient_id}",
            "status": "active" if insured else "none",
            "payor": insurer if insured else "Self-pay (no insurance on file)",
            "beneficiary": f"Patient/{patient_id}"}


def latest_by(items: list[dict[str, Any]], key, date_field: str) -> list[dict[str, Any]]:
    """Keep the most recent item for each key."""
    latest: dict[str, dict[str, Any]] = {}
    for item in sorted(items, key=lambda i: i.get(date_field) or ""):
        latest[key(item)] = item
    return sorted(latest.values(), key=lambda i: i.get(date_field) or "", reverse=True)


def curate_bundle(bundle: dict[str, Any], new_id: str) -> dict[str, Any]:
    resources = [e["resource"] for e in bundle.get("entry", [])]
    by_type: dict[str, list[dict[str, Any]]] = {}
    for r in resources:
        by_type.setdefault(r["resourceType"], []).append(r)

    patient = trim_patient(by_type["Patient"][0], new_id)
    conditions = [trim_condition(c) for c in by_type.get("Condition", [])]
    conditions = latest_by(conditions, lambda c: c["code"]["text"], "onsetDateTime")
    medications = latest_by([trim_medication(m) for m in by_type.get("MedicationRequest", [])],
                            lambda m: m["medication"]["text"], "authoredOn")
    observations = latest_by([trim_observation(o) for o in by_type.get("Observation", [])],
                             lambda o: o["code"]["text"], "effectiveDateTime")
    vitals_first = sorted(observations, key=lambda o: o["category"] != "vital-signs")
    claims = sorted((trim_claim(c) for c in by_type.get("Claim", [])),
                    key=lambda c: c["created"] or "", reverse=True)
    return {
        "patient": patient,
        "conditions": conditions,
        "medications": medications,
        "observations": vitals_first[:MAX_OBSERVATIONS],
        "claims": claims[:MAX_CLAIMS],
        "coverage": coverage_from_claims(new_id, claims),
        "notes": [],
        "source": "Synthea (synthetic data, MITRE, Apache-2.0)",
    }


# --------------------------------------------------------------------------- selection


def candidate_score(bundle: dict[str, Any]) -> tuple[int, int] | None:
    """Score living adults by how much demo-worthy data they have. None = not eligible."""
    resources = [e["resource"] for e in bundle.get("entry", [])]
    patient = next(r for r in resources if r["resourceType"] == "Patient")
    if patient.get("deceasedDateTime"):
        return None
    age = REFERENCE_YEAR - int(patient["birthDate"][:4])
    if not 30 <= age <= 80:
        return None
    active = sum(1 for r in resources if r["resourceType"] == "Condition"
                 and r.get("clinicalStatus", {}).get("coding", [{}])[0].get("code") == "active")
    meds = sum(1 for r in resources if r["resourceType"] == "MedicationRequest"
               and r.get("status") == "active")
    if active < 2 or meds < 1:
        return None
    return active + meds, age


def select(paths: list[Path], count: int) -> list[tuple[Path, dict[str, Any]]]:
    """Richest records first, alternating genders, deterministic for a given download."""
    scored = []
    for path in sorted(paths):
        bundle = json.loads(path.read_text())
        score = candidate_score(bundle)
        if score:
            gender = next(e["resource"]["gender"] for e in bundle["entry"]
                          if e["resource"]["resourceType"] == "Patient")
            scored.append((score[0], path.name, gender, path))
    scored.sort(key=lambda s: (-s[0], s[1]))
    picked, genders = [], {"female": 0, "male": 0}
    for _, _, gender, path in scored:
        if genders.get(gender, 0) < (count + 1) // 2:
            picked.append(path)
            genders[gender] = genders.get(gender, 0) + 1
        if len(picked) == count:
            break
    return [(p, json.loads(p.read_text())) for p in picked]


def is_demo_candidate(bundle: dict[str, Any]) -> bool:
    patient = next(e["resource"] for e in bundle["entry"]
                   if e["resource"]["resourceType"] == "Patient")
    age = REFERENCE_YEAR - int(patient["birthDate"][:4])
    return patient["gender"] == "female" and 45 <= age <= 65


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Curate Synthea patients for the demo")
    parser.add_argument("--raw", type=Path, default=RAW_DIR)
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    parser.add_argument("--count", type=int, default=12)
    args = parser.parse_args(argv)

    paths = [p for p in args.raw.glob("*.json")
             if not p.name.startswith(("hospitalInformation", "practitionerInformation"))]
    chosen = select(paths, args.count)
    demo_index = next((i for i, (_, b) in enumerate(chosen) if is_demo_candidate(b)), 0)

    args.out.mkdir(parents=True, exist_ok=True)
    for old in args.out.glob("*.json"):
        old.unlink()
    for i, (path, bundle) in enumerate(chosen):
        synthea_id = next(e["resource"]["id"] for e in bundle["entry"]
                          if e["resource"]["resourceType"] == "Patient")
        new_id = DEMO_PATIENT_ID if i == demo_index else f"pt-{synthea_id[:8]}"
        record = curate_bundle(bundle, new_id)
        record["synthea_file"] = path.name
        if new_id == DEMO_PATIENT_ID:
            record["patient"]["name"] = [{"family": "Doe", "given": ["Jane"]}]
        (args.out / f"{new_id}.json").write_text(json.dumps(record, indent=2) + "\n")
        name = record["patient"]["name"][0]
        print(f"{new_id:12} {' '.join(name['given'])} {name['family']:<14} "
              f"{record['patient']['gender']:6} born {record['patient']['birthDate']}  "
              f"{len(record['conditions'])} conditions, {len(record['medications'])} meds, "
              f"{record['coverage']['payor']}")
    print(f"wrote {len(chosen)} patients to {args.out} (generated {date.today()})")


if __name__ == "__main__":
    main()
