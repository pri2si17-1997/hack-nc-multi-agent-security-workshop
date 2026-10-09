"""Data shapes for signature requests and emergency (break-the-glass) grants."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from hacknc.common.fingerprint import fingerprint

Status = Literal["pending", "approved", "rejected", "fulfilled", "invalidated", "expired"]
Kind = Literal["prescription", "discharge_summary"]


class Evidence(BaseModel):
    """What the physician sees to make an informed decision (not just a "Sign" button)."""

    sources: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class NewRequest(BaseModel):
    """Body of ``POST /requests``: a draft an agent wants a physician to sign (via ehr-mcp)."""

    kind: Kind
    patient_id: str
    patient_name: str
    title: str = Field(description="one line, e.g. 'Oxycodone 5 mg x 20'")
    content: dict[str, Any] = Field(description="exactly what will be signed and acted on")
    reason: str = ""
    schedule: str | None = Field(default=None, description="controlled-substance schedule")
    agent_chain: list[str] = Field(default_factory=list)
    evidence: Evidence = Field(default_factory=Evidence)

    def content_fingerprint(self) -> str:
        return fingerprint(self.content)


class ApprovalRequest(NewRequest):
    id: str
    fingerprint: str
    status: Status = "pending"
    requested_for: str = Field(description="the human the agents were acting for")
    requested_by: str = Field(description="client that filed it (from the token)")
    created_at: float
    expires_at: float
    decided_by: str | None = None
    decided_at: float | None = None
    decision_note: str = ""
    fulfilled_at: float | None = None
    tampered_submission: dict[str, Any] | None = Field(
        default=None, description="what an agent tried to act on instead (tamper detected)")

    def is_expired(self, now: float) -> bool:
        return self.status in ("pending", "approved") and now > self.expires_at

    def effective(self, now: float) -> ApprovalRequest:
        """The request as it stands at ``now`` (pending/approved past expiry become expired)."""
        return self.model_copy(update={"status": "expired"}) if self.is_expired(now) else self


class Decision(BaseModel):
    note: str = ""


class Fulfilment(BaseModel):
    """Body of ``POST /requests/{id}/consume``: what is about to be acted on, for whom, and
    as what (a signature covers one patient and one kind of document)."""

    content: dict[str, Any]
    patient_id: str | None = None
    kind: Kind | None = None


class BreakGlassRequest(BaseModel):
    patient_id: str
    justification: str


class BreakGlassGrant(BaseModel):
    id: str
    user: str
    role: str | None
    patient_id: str
    justification: str
    created_at: float
    expires_at: float
