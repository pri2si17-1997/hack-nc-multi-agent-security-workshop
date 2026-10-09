from hacknc.approval_service.models import ApprovalRequest, NewRequest
from hacknc.common.fingerprint import fingerprint

RX = {"patient_id": "p1", "drug": "oxycodone", "dose": "5 mg", "quantity": 20}


def test_fingerprint_changes_with_any_field() -> None:
    base = fingerprint(RX)
    assert base == fingerprint(dict(reversed(list(RX.items()))))  # key order doesn't matter
    assert base != fingerprint({**RX, "quantity": 300})
    assert base != fingerprint({**RX, "patient_id": "p2"})


def test_new_request_fingerprints_its_content() -> None:
    req = NewRequest(kind="prescription", patient_id="p1", patient_name="Jane", title="t",
                     content=RX)
    assert req.content_fingerprint() == fingerprint(RX)


def make_request(status="pending", expires_at=100.0) -> ApprovalRequest:
    return ApprovalRequest(kind="prescription", patient_id="p1", patient_name="Jane", title="t",
                           content=RX, id="a1", fingerprint=fingerprint(RX), status=status,
                           requested_for="dr.morgana", requested_by="ehr-mcp",
                           created_at=0.0, expires_at=expires_at)


def test_pending_and_approved_requests_expire() -> None:
    assert make_request("pending").effective(now=200).status == "expired"
    assert make_request("approved").effective(now=200).status == "expired"
    assert make_request("pending").effective(now=50).status == "pending"


def test_final_states_never_expire() -> None:
    for status in ("fulfilled", "rejected", "invalidated"):
        assert make_request(status).effective(now=10_000).status == status
