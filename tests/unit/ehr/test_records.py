from pathlib import Path

import pytest

from hacknc.ehr.records import EhrRepository, PatientNotFound, security_labels

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def repo() -> EhrRepository:
    return EhrRepository.load(ROOT / "data" / "curated", ROOT / "data" / "overlay")


def test_twelve_patients_all_admitted_in_bed_order(repo) -> None:
    assert len(repo) == 12
    ward = repo.search()
    assert all(r.admission() for r in ward)
    beds = [r.bed for r in ward]
    assert beds == sorted(beds) and len(set(beds)) == 12


def test_generated_admissions_can_be_left_out(repo) -> None:
    hand_only = EhrRepository.load(ROOT / "data" / "curated", ROOT / "data" / "overlay",
                                   generated=False)
    assert [r.id for r in hand_only.search() if r.admission()] == ["jane-doe"]


def test_jane_is_admitted_for_pneumonia(repo) -> None:
    jane = repo.get("jane-doe")
    stay = jane.admission()
    assert stay and "pneumonia" in stay["reason"].lower()
    assert jane.demographics()["admission"]["location"] == "4 West, bed 12"


def test_hospital_and_home_meds_and_holds(repo) -> None:
    meds = repo.get("jane-doe").current_medications()
    categories = {m["medication"]["text"].split()[0]: m["category"] for m in meds}
    assert categories["Ceftriaxone"] == "inpatient"
    assert categories["Oxycodone"] == "home"
    metformin = next(m for m in meds if "Metformin" in m["medication"]["text"])
    assert metformin["status"] == "on-hold" and "contrast" in metformin["holdReason"]


def test_sensitive_labels(repo) -> None:
    jane = repo.get("jane-doe")
    psych = next(n for n in jane.notes if n["id"] == "ov-jd-note-psych")
    assert security_labels(psych) == ["PSY"]
    # The prompt-injection message is staged by the EHR gateway, not stored in the chart.
    assert [m["id"] for m in jane.messages] == ["msg-jd-1"]


def test_preferences_and_allergy(repo) -> None:
    jane = repo.get("jane-doe")
    assert jane.preferences["consent"]["shareSensitiveWithPcp"] is False
    assert jane.allergies[0]["drugClass"] == "penicillin"


def test_posting_and_resetting_instructions(repo) -> None:
    repo.post_instructions("jane-doe", "Rest well", "instructions-agent")
    assert repo.get("jane-doe").posted_instructions[0]["text"] == "Rest well"
    repo.reset_runtime_state()
    assert repo.get("jane-doe").posted_instructions == []


def test_unknown_patient(repo) -> None:
    with pytest.raises(PatientNotFound):
        repo.get("nobody")


def test_portal_account_is_linked_to_one_record(repo) -> None:
    assert repo.patient_for_account("jane.doe") == "jane-doe"
    assert repo.patient_for_account("dr.morgana") is None


def test_accepted_dose_change_joins_the_discharge_plan_and_reset_restores(repo) -> None:
    request = repo.submit_request("jane-doe", "medication_change", "Raise my oxycodone",
                                  "jane.doe", drug="oxycodone", requested_dose="10 mg")
    assert repo.get("jane-doe").open_requests() == [request]
    decided = repo.decide_request("jane-doe", request["id"], True, "dr.morgana", "short term")
    assert decided["status"] == "accepted"
    plan = repo.get("jane-doe").admission()["dischargePlan"]["medications"]
    oxy = [m for m in plan if m["drug"] == "oxycodone"]
    assert len(oxy) == 1 and oxy[0]["dose"] == "10 mg" and oxy[0]["action"] == "renew"
    assert "accepted by dr.morgana" in oxy[0]["reason"]
    with pytest.raises(ValueError):
        repo.decide_request("jane-doe", request["id"], False, "dr.morgana")   # once
    repo.set_consent("jane-doe", True, "jane.doe")
    repo.post_portal_message("jane-doe", "hello", "jane.doe")
    repo.reset_runtime_state()
    jane = repo.get("jane-doe")
    assert jane.requests == [] and jane.preferences["consent"]["shareSensitiveWithPcp"] is False
    assert [m for m in jane.admission()["dischargePlan"]["medications"]
            if m["drug"] == "oxycodone"][0]["dose"] == "5 mg"
    assert not any(m["text"] == "hello" for m in jane.messages)


def test_accepted_request_without_any_dose_leaves_the_plan_alone(repo) -> None:
    """'Raise my insulin': no number in the request, none in the record. No prescription with
    an empty dose goes into the plan; the physician sets it."""
    pid = "pt-982beb81"   # Merlin Nienow: a mixed insulin with no dose on file
    before = list(repo.get(pid).admission()["dischargePlan"].get("medications", []))
    request = repo.submit_request(pid, "medication_change", "Raise my insulin",
                                  "merlin.nienow", drug="insulin")
    decided = repo.decide_request(pid, request["id"], True, "dr.morgana")
    assert decided["status"] == "accepted" and decided["plan_item"] is None
    assert repo.get(pid).admission()["dischargePlan"].get("medications", []) == before
    repo.reset_runtime_state()


def test_declined_request_changes_nothing(repo) -> None:
    before = list(repo.get("jane-doe").admission()["dischargePlan"]["medications"])
    request = repo.submit_request("jane-doe", "medication_change", "Stop metformin",
                                  "jane.doe", drug="metformin")
    repo.decide_request("jane-doe", request["id"], False, "dr.morgana", "keep taking it")
    assert repo.get("jane-doe").admission()["dischargePlan"]["medications"] == before
    repo.reset_runtime_state()
