import json
from pathlib import Path

from hacknc.agents import compose
from hacknc.ehr.records import EhrRepository, security_labels
from hacknc.tools import generate_admissions as gen

ROOT = Path(__file__).resolve().parents[3]
ACTIONS = {"new", "renew", "resume", "stop"}


def test_deterministic_and_matches_the_committed_files() -> None:
    first, second = gen.generate(), gen.generate()
    assert first == second
    committed = {p.stem: json.loads(p.read_text())
                 for p in (ROOT / "data" / "overlay" / "generated").glob("*.json")}
    assert committed == first, "run `make admissions` and commit data/overlay/generated/"


def test_a_different_seed_changes_the_details() -> None:
    assert gen.generate(seed=1) != gen.generate()


def test_eleven_admissions_with_the_generic_plan_schema() -> None:
    admissions = gen.generate()
    assert len(admissions) == 11 and "jane-doe" not in admissions
    for overlay in admissions.values():
        [stay] = overlay["encounters"]
        assert stay["status"] == "in-progress" and stay["location"].startswith("4 West, bed ")
        plan = stay["dischargePlan"]
        assert set(plan) == {"readyDate", "medications", "equipment", "followUp",
                             "warningSigns"}
        for med in plan["medications"]:
            assert med["action"] in ACTIONS and med["drug"]
            if med["action"] == "new":
                assert {"dose", "quantity", "sig", "reason"} <= set(med)
        assert {"specialty", "within_days", "reason"} <= set(plan["followUp"])
        obs_ids = {o["id"] for o in overlay["observations"]}
        for item in plan["equipment"]:
            assert set(item["evidence"]) <= obs_ids
        assert overlay["notes"] and overlay["messages"]
        assert all("generated-admission" in {t["code"] for t in r["meta"]["tag"]}
                   for r in overlay["conditions"] + overlay["observations"])


def test_reason_rules_follow_the_patients_conditions() -> None:
    reasons = {pid: o["encounters"][0]["reason"] for pid, o in gen.generate().items()}
    assert "pneumonia" in reasons["pt-87c46349"].lower()      # the HIV-labelled patient
    assert reasons["pt-4d2cc5fb"] == "Breakthrough seizure"     # seizure disorder
    assert "kidney" in reasons["pt-6e38fda8"].lower()           # CKD


def test_every_patient_reconciles_with_the_rules() -> None:
    repo = EhrRepository.load(ROOT / "data" / "curated", ROOT / "data" / "overlay")
    for record in repo.search():
        plan = record.admission()["dischargePlan"]
        result = compose.reconcile(record.current_medications(), record.allergies,
                                   record.observations, plan)
        assert result["discharge_medications"], record.id
        for item in result["discharge_medications"]:
            assert item["action"] in {"new", "renew", "resume", "continue", "stop", "hold"}


def test_hiv_record_stays_labelled_and_is_never_in_the_plan() -> None:
    repo = EhrRepository.load(ROOT / "data" / "curated", ROOT / "data" / "overlay")
    kermit = repo.get("pt-87c46349")
    assert any(security_labels(c) == ["HIV"] for c in kermit.conditions)
    assert "HIV" not in json.dumps(kermit.admission()["dischargePlan"])
