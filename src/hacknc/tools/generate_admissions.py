"""Admit every Synthea patient to 4 West: generate a realistic current hospital stay for each.

Jane Doe's admission is hand-written (``data/overlay/jane-doe.json``). The other eleven come
from here: the admission reason is picked from each patient's own conditions with a small
rule table, then filled in from a template:

    HIV (labelled)          -> community-acquired pneumonia (home oxygen; HIV stays labelled)
    seizure disorder        -> breakthrough seizure
    prostate cancer / UTI   -> urosepsis
    osteoporosis            -> hip fracture (surgery done; walker, short opioid course)
    coronary disease        -> NSTEMI
    chronic kidney disease  -> acute kidney injury
    diabetes                -> hyperglycemic crisis

Each admission gets an encounter (bed, attending, discharge plan), the admission diagnoses
(ICD-10), hospital-only medications, holds on home medications, labs, a progress note, a
patient-portal message, sometimes an allergy, and the patient's pharmacy / family doctor /
consent. Everything is tagged ``workshop-overlay`` and ``generated-admission``.

The output is deterministic (``--seed``) and committed, so the demo never needs this step:

    make admissions        # writes data/overlay/generated/<patient-id>.json
"""

from __future__ import annotations

import argparse
import json
import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from hacknc.ehr.records import EhrRepository, PatientRecord, security_labels

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "data"
OUT_DIR = DATA / "overlay" / "generated"

WARD = "4 West"
JANES_BED = 12
BEDS = [b for b in range(1, 17) if b != JANES_BED]
ATTENDING = "Dr. Morgana Pendragon"
WARD_DAY = date(2026, 10, 3)       # the day of the demo story (Jane's discharge day)
DEFAULT_SEED = 2026

ICD10 = "http://hl7.org/fhir/sid/icd-10-cm"
LOINC = "http://loinc.org"
TAGS = [{"code": "workshop-overlay"}, {"code": "generated-admission"}]

# drugClass is what medication reconciliation compares against planned medicines.
ALLERGY_POOL = [
    None, None, None, None,
    {"substance": "Penicillin", "drugClass": "penicillin", "reaction": "Hives",
     "severity": "moderate"},
    {"substance": "Sulfamethoxazole (sulfa drugs)", "drugClass": "sulfonamide",
     "reaction": "Rash", "severity": "moderate"},
    {"substance": "Latex", "drugClass": "", "reaction": "Contact dermatitis",
     "severity": "mild"},
]
SULFA = ALLERGY_POOL[5]
PLAN_NAMES = {
    "Medicare": "Original Medicare (Parts A & B)", "Medicaid": "NC Medicaid Managed Care",
    "Aetna": "Aetna Choice POS II", "UnitedHealthcare": "UnitedHealthcare Choice Plus",
    "Blue Cross Blue Shield": "PPO Gold",
}


# --------------------------------------------------------------------------- templates


@dataclass
class Template:
    """One kind of admission. Strings may use {first}, {age}, {sex} and {lab:<key>}."""

    key: str
    reason: str
    diagnoses: list[tuple[str, str]]                 # (ICD-10 code, text)
    stay_days: tuple[int, int]
    inpatient: list[dict[str, Any]]                  # hospital-only medicines
    holds: list[dict[str, Any]]                      # home medicines held on admission
    labs: list[dict[str, Any]]                       # key, text, range or value, unit, ...
    progress: str
    plan_medications: list[dict[str, Any]]
    follow_up: dict[str, Any]
    warning_signs: str
    portal: str
    equipment: list[dict[str, Any]] = field(default_factory=list)
    allergies: list[dict[str, Any] | None] | None = None   # pool to pick from; default ALLERGY_POOL


def lab(key: str, text: str, low: float, high: float, unit: str, *, category: str = "laboratory",
        loinc: str = "", digits: int = 1, interpretation: str = "") -> dict[str, Any]:
    return {"key": key, "text": text, "range": (low, high), "unit": unit, "category": category,
            "loinc": loinc, "digits": digits, "interpretation": interpretation}


TEMPLATES: dict[str, Template] = {t.key: t for t in [
    Template(
        key="pneumonia", reason="Community-acquired pneumonia, left lower lobe",
        diagnoses=[("J18.9", "Community-acquired pneumonia, left lower lobe"),
                   ("R09.02", "Hypoxemia on exertion")],
        stay_days=(3, 4),
        inpatient=[{"text": "Ceftriaxone 1 g IV every 24 hours", "drugClass": "cephalosporin",
                    "dischargeAction": "switch to oral antibiotic"},
                   {"text": "Azithromycin 500 mg IV daily", "dischargeAction":
                    "stop (3-day course complete)"}],
        holds=[],
        labs=[lab("spo2walk", "Oxygen saturation, walking on room air", 85, 88, "%",
                  category="vital-signs", loinc="59408-5", digits=0,
                  interpretation="low: qualifies for home oxygen (≤ 88%)"),
              lab("spo2rest", "Oxygen saturation at rest", 93, 95, "%", category="vital-signs",
                  loinc="59408-5", digits=0),
              lab("wbc", "White blood cells", 8.0, 10.5, "10*3/uL",
                  interpretation="normal (14.2 on admission)"),
              lab("creat", "Creatinine", 0.8, 1.1, "mg/dL", interpretation="normal")],
        progress="Afebrile 48 h, cough improving, WBC normalised. Walking SpO2 {lab:spo2walk} "
                 "on room air: needs home oxygen with exertion. Plan: discharge; switch to oral "
                 "antibiotic to complete 5 more days; continue home medicines; family doctor "
                 "within 7 days; repeat chest X-ray in 6 weeks.",
        plan_medications=[{"action": "new", "drug": "amoxicillin-clavulanate",
                           "dose": "875/125 mg", "quantity": 10,
                           "sig": "1 tablet by mouth twice daily for 5 days",
                           "reason": "complete pneumonia treatment (switch from IV)"}],
        equipment=[{"item": "Home oxygen", "detail": "2 L/min by nasal cannula with exertion",
                    "prior_auth": True, "evidence": ["spo2walk"],
                    "diagnosis_codes": ["J18.9", "R09.02"]}],
        follow_up={"specialty": "primary care", "within_days": 7,
                   "reason": "Hospital follow-up after pneumonia",
                   "note": "repeat chest X-ray in 6 weeks"},
        warning_signs="trouble breathing, fever over 38.5 °C, or chest pain",
        portal="Hi Dr. Pendragon, will I have to use the oxygen all the time at home? And can my "
               "prescriptions go to Main Street Pharmacy like usual? Thanks, {first}"),
    Template(
        key="seizure", reason="Breakthrough seizure",
        diagnoses=[("G40.909", "Epilepsy with breakthrough seizure")],
        stay_days=(2, 3),
        inpatient=[{"text": "Levetiracetam 1000 mg IV every 12 hours",
                    "dischargeAction": "switch to oral levetiracetam"},
                   {"text": "Lorazepam 2 mg IV as needed for seizure over 5 minutes",
                    "controlledSchedule": "C-IV", "dischargeAction": "stop (hospital only)"}],
        holds=[],
        labs=[lab("na", "Sodium", 136, 140, "mmol/L", digits=0, interpretation="normal"),
              lab("glucose", "Glucose", 95, 130, "mg/dL", digits=0),
              lab("mg", "Magnesium", 1.9, 2.2, "mg/dL", interpretation="normal")],
        progress="No further seizures in 48 h on IV levetiracetam. EEG: no ongoing seizure "
                 "activity. Missed doses of the home regimen the likely trigger. Plan: "
                 "discharge on oral levetiracetam; seizure precautions; no driving for 6 "
                 "months (NC law); neurology follow-up within 2 weeks.",
        plan_medications=[{"action": "new", "drug": "levetiracetam", "dose": "500 mg",
                           "quantity": 60, "sig": "1 tablet by mouth twice daily",
                           "reason": "prevent further seizures (switch from IV)"}],
        follow_up={"specialty": "neurology", "within_days": 14,
                   "reason": "Seizure follow-up after a breakthrough seizure",
                   "note": "no driving until cleared by neurology"},
        warning_signs="a seizure lasting more than 5 minutes, or two seizures in a row",
        portal="Dr. Pendragon, my daughter is picking me up. Am I allowed to drive myself to the "
               "pharmacy next week? - {first}"),
    Template(
        key="urosepsis", reason="Urosepsis (E. coli)",
        diagnoses=[("A41.51", "Sepsis due to Escherichia coli"),
                   ("N39.0", "Urinary tract infection")],
        stay_days=(3, 5),
        inpatient=[{"text": "Ceftriaxone 1 g IV every 24 hours", "drugClass": "cephalosporin",
                    "dischargeAction": "switch to oral antibiotic"},
                   {"text": "Sodium chloride 0.9% IV 100 mL/hour",
                    "dischargeAction": "stop (hospital only)"}],
        holds=[{"match": "hydrochlorothiazide", "reason": "Held on admission: low blood "
                "pressure with sepsis", "resumeIf": {"observation": "systolic", "min": 110}}],
        labs=[lab("sbp", "Systolic blood pressure", 118, 132, "mm[Hg]", category="vital-signs",
                  loinc="8480-6", digits=0),
              lab("lactate", "Lactate", 1.0, 1.6, "mmol/L", interpretation="normal (3.4 on "
                  "admission)"),
              lab("wbc", "White blood cells", 8.5, 11.0, "10*3/uL"),
              {"key": "culture", "text": "Urine culture", "value": "E. coli >100,000 CFU/mL: "
               "sensitive to trimethoprim-sulfamethoxazole and cefpodoxime; resistant to "
               "ciprofloxacin", "category": "laboratory", "loinc": "630-4"}],
        progress="Afebrile 48 h, blood pressure normal off fluids, lactate cleared. Urine "
                 "culture E. coli, sensitive to trimethoprim-sulfamethoxazole. Plan: discharge "
                 "on oral antibiotic to complete 10 days; restart blood-pressure tablet; family "
                 "doctor within 7 days.",
        plan_medications=[{"action": "new", "drug": "trimethoprim-sulfamethoxazole",
                           "dose": "800/160 mg", "quantity": 14,
                           "sig": "1 tablet by mouth twice daily for 7 days",
                           "reason": "complete urosepsis treatment (culture-sensitive)"},
                          {"action": "resume", "drug": "hydrochlorothiazide",
                           "reason": "blood pressure back to normal"}],
        follow_up={"specialty": "primary care", "within_days": 7,
                   "reason": "Hospital follow-up after urosepsis",
                   "note": "repeat urine test if symptoms return"},
        allergies=[SULFA, SULFA, None],
        warning_signs="fever over 38.5 °C, shaking chills, confusion, or pain when passing "
                      "urine",
        portal="Hello, this is {first}'s daughter (proxy access). Does Dad still need to "
               "drink extra water at home, and when does the antibiotic end?"),
    Template(
        key="hip_fracture", reason="Right hip fracture, after hemiarthroplasty",
        diagnoses=[("S72.001A", "Fracture of neck of right femur"),
                   ("Z96.641", "Right hip hemiarthroplasty, post-operative day 3")],
        stay_days=(4, 5),
        inpatient=[{"text": "Morphine 2 mg IV every 4 hours as needed for pain",
                    "controlledSchedule": "C-II", "dischargeAction": "stop (hospital only)"},
                   {"text": "Cefazolin 2 g IV every 8 hours (surgical prophylaxis)",
                    "dischargeAction": "stop (completed)"}],
        holds=[],
        labs=[lab("hgb", "Hemoglobin", 9.4, 10.6, "g/dL",
                  interpretation="low after surgery, stable"),
              lab("creat", "Creatinine", 0.7, 1.0, "mg/dL", interpretation="normal"),
              lab("vitd", "25-hydroxy vitamin D", 14, 19, "ng/mL", digits=0,
                  interpretation="low")],
        progress="Post-operative day 3 after right hip hemiarthroplasty. Walking 30 m with a "
                 "walker and physical therapy. Pain controlled on oral medicines. Plan: "
                 "discharge home with walker and home physical therapy; clot prevention for 4 "
                 "weeks; short oxycodone course; vitamin D; orthopedics within 2 weeks.",
        plan_medications=[{"action": "new", "drug": "enoxaparin", "dose": "40 mg",
                           "quantity": 28, "sig": "inject under the skin once daily for 4 weeks",
                           "reason": "clot prevention after hip surgery"},
                          {"action": "new", "drug": "oxycodone", "dose": "5 mg",
                           "quantity": 12, "sig": "1 tablet every 6 hours as needed for "
                           "severe pain", "reason": "short course for post-operative pain"},
                          {"action": "new", "drug": "acetaminophen", "dose": "1000 mg",
                           "quantity": 90, "sig": "1 dose by mouth three times daily",
                           "reason": "first-line pain relief"},
                          {"action": "new", "drug": "cholecalciferol", "dose": "2000 units",
                           "quantity": 30, "sig": "1 tablet by mouth daily",
                           "reason": "low vitamin D, fracture prevention"}],
        equipment=[{"item": "Front-wheeled walker", "detail": "for walking at home",
                    "prior_auth": False, "evidence": [], "diagnosis_codes": ["S72.001A"]}],
        follow_up={"specialty": "orthopedics", "within_days": 14,
                   "reason": "Wound check and X-ray after hip surgery",
                   "note": "home physical therapy 3 times a week"},
        warning_signs="a hot, swollen calf, the wound leaking or red, chest pain or sudden "
                      "shortness of breath",
        portal="Will someone help me with the stairs at home? My bedroom is upstairs. - {first}"),
    Template(
        key="nstemi", reason="Non-ST-elevation myocardial infarction (NSTEMI)",
        diagnoses=[("I21.4", "Non-ST elevation myocardial infarction"),
                   ("I25.10", "Coronary artery disease, stent to the circumflex artery")],
        stay_days=(3, 4),
        inpatient=[{"text": "Heparin infusion per protocol",
                    "dischargeAction": "stop (hospital only)"},
                   {"text": "Nitroglycerin infusion 10 mcg/min",
                    "dischargeAction": "stop (hospital only)"}],
        holds=[{"match": "metformin", "reason": "Held for the cardiac catheterisation (contrast)",
                "resumeIf": {"observation": "creatinine", "max": 1.3}}],
        labs=[lab("trop", "Troponin I, high sensitivity (peak)", 820, 1600, "ng/L", digits=0,
                  interpretation="high (heart muscle injury)"),
              lab("ldl", "LDL cholesterol", 128, 162, "mg/dL", digits=0, interpretation="high"),
              lab("creat", "Creatinine", 0.8, 1.1, "mg/dL", interpretation="normal")],
        progress="Cardiac catheterisation day 1: drug-eluting stent to the circumflex artery. "
                 "Chest-pain free since. Ejection fraction 50%. Plan: discharge on dual "
                 "antiplatelet therapy, beta-blocker and high-intensity statin (replaces "
                 "simvastatin); restart metformin if kidney function normal; cardiology within "
                 "7 days; cardiac rehab referral.",
        plan_medications=[{"action": "new", "drug": "aspirin", "dose": "81 mg", "quantity": 90,
                           "sig": "1 tablet by mouth daily", "reason": "protect the new stent"},
                          {"action": "new", "drug": "metoprolol succinate", "dose": "25 mg",
                           "quantity": 30, "sig": "1 tablet by mouth daily",
                           "reason": "after heart attack"},
                          {"action": "new", "drug": "atorvastatin", "dose": "80 mg",
                           "quantity": 30, "sig": "1 tablet by mouth at night",
                           "reason": "high-intensity statin after heart attack"},
                          {"action": "stop", "drug": "simvastatin",
                           "reason": "replaced by atorvastatin"},
                          {"action": "resume", "drug": "metformin",
                           "reason": "kidney function normal after contrast"}],
        follow_up={"specialty": "cardiology", "within_days": 7,
                   "reason": "Follow-up after NSTEMI and coronary stent",
                   "note": "cardiac rehabilitation referral sent"},
        warning_signs="chest pain or pressure that lasts more than 5 minutes (call 911), "
                      "fainting, or black stools",
        portal="When can I go back to work? I drive a delivery van. - {first}"),
    Template(
        key="aki", reason="Acute kidney injury on chronic kidney disease",
        diagnoses=[("N17.9", "Acute kidney injury"),
                   ("E86.0", "Dehydration")],
        stay_days=(3, 4),
        inpatient=[{"text": "Sodium chloride 0.9% IV 125 mL/hour",
                    "dischargeAction": "stop (hospital only)"}],
        holds=[{"match": "naproxen", "reason": "Held on admission: NSAIDs worsen kidney injury",
                "resumeIf": {"observation": "creatinine", "max": 1.3}},
               {"match": "hydrochlorothiazide", "reason": "Held on admission: dehydration",
                "resumeIf": {"observation": "creatinine", "max": 1.3}},
               {"match": "metformin", "reason": "Held on admission: kidney function reduced",
                "resumeIf": {"observation": "creatinine", "max": 1.3}}],
        labs=[lab("creat", "Creatinine", 1.5, 1.8, "mg/dL",
                  interpretation="high (2.6 on admission; baseline 1.1)"),
              lab("k", "Potassium", 4.2, 4.9, "mmol/L", interpretation="normal"),
              lab("bun", "Urea nitrogen", 24, 31, "mg/dL", digits=0)],
        progress="Creatinine improving with IV fluids, {lab:creat} today from 2.6 on admission "
                 "(baseline 1.1). Eating and drinking well. Plan: discharge; stop naproxen for "
                 "good; restart hydrochlorothiazide; repeat kidney tests in 3 days; nephrology "
                 "within 2 weeks.",
        plan_medications=[{"action": "stop", "drug": "naproxen",
                           "reason": "NSAIDs harm the kidneys: avoid"},
                          {"action": "resume", "drug": "hydrochlorothiazide",
                           "reason": "restart blood-pressure tablet"},
                          {"action": "new", "drug": "acetaminophen", "dose": "500 mg",
                           "quantity": 60, "sig": "1 to 2 tablets every 8 hours as needed",
                           "reason": "pain relief instead of naproxen"}],
        follow_up={"specialty": "nephrology", "within_days": 14,
                   "reason": "Kidney follow-up after acute kidney injury",
                   "note": "repeat kidney blood tests in 3 days"},
        warning_signs="passing much less urine, swollen legs, or feeling very dizzy",
        portal="Can I keep taking my naproxen for my hand pain? It's the only thing that "
               "helps. - {first}"),
    Template(
        key="hyperglycemia", reason="Hyperglycemic crisis, type 2 diabetes",
        diagnoses=[("E11.65", "Type 2 diabetes with hyperglycemia"),
                   ("E86.0", "Dehydration")],
        stay_days=(2, 3),
        inpatient=[{"text": "Insulin regular infusion per protocol",
                    "dischargeAction": "stop (hospital only)"},
                   {"text": "Sodium chloride 0.9% IV 150 mL/hour",
                    "dischargeAction": "stop (hospital only)"}],
        holds=[],
        labs=[lab("glucose", "Glucose", 150, 190, "mg/dL", digits=0,
                  interpretation="improved (612 on admission)"),
              lab("a1c", "Hemoglobin A1c", 10.8, 12.1, "%", interpretation="high"),
              lab("creat", "Creatinine", 0.8, 1.1, "mg/dL", interpretation="normal")],
        progress="Glucose controlled on basal insulin, eating normally, rehydrated. A1c "
                 "{lab:a1c}. Seen twice by the diabetes educator. Plan: discharge on long-acting "
                 "insulin plus metformin; glucose meter; endocrinology within 2 weeks.",
        plan_medications=[{"action": "new", "drug": "insulin glargine", "dose": "18 units",
                           "quantity": 5, "sig": "inject 18 units under the skin at bedtime",
                           "reason": "long-acting insulin for high sugars"}],
        equipment=[{"item": "Glucose meter and test strips", "detail": "check sugar before "
                    "breakfast and dinner", "prior_auth": False, "evidence": [],
                    "diagnosis_codes": ["E11.65"]}],
        follow_up={"specialty": "endocrinology", "within_days": 14,
                   "reason": "Diabetes follow-up after a hyperglycemic crisis",
                   "note": "bring the glucose log"},
        warning_signs="sugar over 300 twice in a row, sugar under 70 with shaking or sweating, "
                      "or vomiting",
        portal="How often do I need to check my sugar, and what number is too low? - {first}"),
]}

# First match wins. Each rule: (template key, words to look for in active conditions).
RULES: list[tuple[str, tuple[str, ...]]] = [
    ("pneumonia", ("hiv", "human immunodeficiency")),
    ("seizure", ("seizure", "epilepsy")),
    ("urosepsis", ("prostate", "urinary tract infection")),
    ("hip_fracture", ("osteoporosis",)),
    ("nstemi", ("coronary",)),
    ("aki", ("chronic kidney disease",)),
    ("hyperglycemia", ("diabetes",)),
]
FALLBACK = "pneumonia"


def pick_template(record: PatientRecord) -> Template:
    """The admission reason, from the patient's own active conditions (labelled ones too)."""
    text = " ".join(c["code"]["text"].lower() for c in record.active_conditions())
    for key, words in RULES:
        if any(w in text for w in words):
            return TEMPLATES[key]
    return TEMPLATES[FALLBACK]


# --------------------------------------------------------------------------- one admission


def _fill(text: str, ctx: dict[str, str]) -> str:
    for k, v in ctx.items():
        text = text.replace("{" + k + "}", v)
    return text


def _meta() -> dict[str, Any]:
    return {"tag": [dict(t) for t in TAGS]}


def build_admission(record: PatientRecord, bed: int, rng: random.Random) -> dict[str, Any]:
    """The overlay for one patient (same shape as ``data/overlay/jane-doe.json``)."""
    t = pick_template(record)
    short = record.id.removeprefix("pt-")[:4]
    prefix = f"gen-{short}"
    stay = rng.randint(*t.stay_days)
    start = WARD_DAY - timedelta(days=stay)
    admitted = f"{start.isoformat()}T{rng.randint(8, 22):02d}:{rng.choice(['05', '20', '40'])}:00"
    first = record.display_name.split()[0]

    observations, values = [], {}
    for item in t.labs:
        if "value" in item:
            value = item["value"]
        else:
            low, high = item["range"]
            number = round(rng.uniform(low, high), item["digits"])
            number = int(number) if item["digits"] == 0 else number
            value = f"{number} {item['unit']}"
        values[item["key"]] = value
        obs = {"resourceType": "Observation", "id": f"{prefix}-obs-{item['key']}",
               "category": item["category"], "code": {"text": item["text"]}, "value": value,
               "effectiveDateTime": f"{WARD_DAY.isoformat()}T0{rng.randint(5, 7)}:"
                                    f"{rng.choice(['00', '15', '30'])}:00",
               "meta": _meta()}
        if item.get("loinc"):
            obs["code"]["coding"] = [{"system": LOINC, "code": item["loinc"]}]
        if item.get("interpretation"):
            obs["interpretation"] = item["interpretation"]
        observations.append(obs)
    ctx = {"first": first, **{f"lab:{k}": v for k, v in values.items()}}

    conditions = [{"resourceType": "Condition", "id": f"{prefix}-cond-{i}",
                   "clinicalStatus": "active",
                   "code": {"text": text, "coding": [{"system": ICD10, "code": code,
                                                      "display": text}]},
                   "onsetDateTime": start.isoformat(), "meta": _meta()}
                  for i, (code, text) in enumerate(t.diagnoses, 1)]

    medications = [{"resourceType": "MedicationRequest", "id": f"{prefix}-med-ip{i}",
                    "status": "active", "category": "inpatient",
                    "medication": {"text": m["text"]}, "authoredOn": start.isoformat(),
                    **{k: v for k, v in m.items() if k != "text"}, "meta": _meta()}
                   for i, m in enumerate(t.inpatient, 1)]

    home = " ".join(m["medication"]["text"].lower() for m in record.medications
                    if m.get("status") == "active")
    holds = [h for h in t.holds if h["match"] in home]
    held = {h["match"] for h in holds}
    plan_meds = [dict(m) for m in t.plan_medications
                 if m["action"] not in ("resume", "stop") or m["drug"] in held
                 or m["drug"] in home]

    allergy = rng.choice(t.allergies or ALLERGY_POOL)
    allergies = [{"resourceType": "AllergyIntolerance", "id": f"{prefix}-alg-1",
                  **allergy, "recorded": f"{rng.randint(1995, 2018)}-0{rng.randint(1, 9)}-1"
                  f"{rng.randint(0, 9)}", "meta": _meta()}] if allergy else []

    equipment = [{**e, "evidence": [f"{prefix}-obs-{k}" for k in e["evidence"]]}
                 for e in t.equipment]
    sensitive = any(security_labels(r) for r in record.conditions + record.medications)

    encounter = {
        "resourceType": "Encounter", "id": f"{prefix}-enc", "status": "in-progress",
        "class": "inpatient", "reason": t.reason, "period": {"start": admitted},
        "location": f"{WARD}, bed {bed}", "attending": ATTENDING,
        "dischargePlan": {
            "readyDate": WARD_DAY.isoformat(),
            "medications": plan_meds,
            "equipment": equipment,
            "followUp": dict(t.follow_up),
            "warningSigns": t.warning_signs,
        },
        "meta": _meta()}
    note = {"resourceType": "DocumentReference", "id": f"{prefix}-note-progress",
            "date": WARD_DAY.isoformat(), "type": {"text": f"Progress note, hospital day {stay}"},
            "author": ATTENDING, "text": _fill(t.progress, ctx), "meta": _meta()}
    message = {"resourceType": "Communication", "id": f"{prefix}-msg-1",
               "sent": f"{(WARD_DAY - timedelta(days=1)).isoformat()}T{rng.randint(18, 22)}:"
                       f"{rng.randint(10, 59)}:00",
               "sender": f"{record.display_name} (patient portal)",
               "text": _fill(t.portal, ctx), "meta": _meta()}

    payor = (record.coverage or {}).get("payor", "Medicare")
    coverage = {"resourceType": "Coverage", "id": f"cov-{record.id}", "status": "active",
                "payor": payor, "plan": PLAN_NAMES.get(payor, "Standard plan"),
                "memberId": f"{payor[:3].upper()}-{rng.randint(100, 999)}-"
                            f"{rng.randint(1000, 9999)}",
                "beneficiary": f"Patient/{record.id}", "meta": _meta()}

    return {
        "_story": f"{record.display_name}, {record.age}. Admitted {start.isoformat()} with "
                  f"{t.reason.lower()} (generated from: {t.key} rule).",
        "encounters": [encounter], "conditions": conditions, "medications": medications,
        "medicationHolds": holds, "observations": observations, "notes": [note],
        "messages": [message], "allergies": allergies, "coverage": coverage,
        "preferences": {
            "pharmacy": {"partner": "retail-pharmacy", "name": "Main Street Pharmacy",
                         "address": "212 Elm St, Durham, NC"},
            "primaryCare": {"partner": "pcp-clinic", "name": "Maple Family Medicine",
                            "clinician": "Dr. Alex Lee"},
            "consent": {"shareSensitiveWithPcp": False,
                        "note": "No consent on file to share sensitive records outside the "
                                "hospital" if sensitive else "No sensitive records on file"},
            "portalAccount": record.portal_username},
    }


def generate(seed: int = DEFAULT_SEED, data_dir: Path = DATA) -> dict[str, dict[str, Any]]:
    """Admissions for everyone not already admitted by a hand-written overlay."""
    repo = EhrRepository.load(data_dir / "curated", data_dir / "overlay", generated=False)
    rng = random.Random(seed)
    waiting = [r for r in sorted(repo.search(), key=lambda r: r.id) if r.admission() is None]
    beds = rng.sample(BEDS, len(waiting))
    return {r.id: build_admission(r, bed, random.Random(f"{seed}:{r.id}"))
            for r, bed in zip(waiting, beds, strict=True)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    for old in args.out.glob("*.json"):
        old.unlink()
    for pid, overlay in generate(args.seed).items():
        (args.out / f"{pid}.json").write_text(json.dumps(overlay, indent=1, ensure_ascii=False)
                                              + "\n")
        print(f"{pid}: {overlay['_story']}")


if __name__ == "__main__":
    main()
