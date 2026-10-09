"""HTTP API of the mock partners. Every request needs a token addressed to that partner.

    POST /payer/eligibility            Blue Cross Blue Shield: is the member covered?
    POST /payer/prior-auth             ... approve home oxygen? (needs a qualifying SpO2 <= 88%)
    POST /nc-csrs/query                NC Controlled Substances Reporting System: fill history
    POST /retail-pharmacy/prescriptions  Main Street Pharmacy: accept a signed e-prescription
                                       (re-checks the signature fingerprint itself)
    POST /pcp-clinic/appointments      Maple Family Medicine: book a follow-up
    POST /pcp-clinic/documents         ... receive the discharge summary
    GET  /admin/received               what each partner received (X-Demo-Admin-Key)
    DELETE /admin/received             forget it (demo reset)
"""

from __future__ import annotations

import re
import secrets
import uuid
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from opentelemetry import trace

from hacknc.common.auth import JwtValidator, TokenClaims, TokenValidationError
from hacknc.common.fingerprint import fingerprint
from hacknc.common.telemetry import FASTAPI_TELEMETRY

PARTNER_NAMES = {
    "payer": "Blue Cross Blue Shield",
    "retail-pharmacy": "Main Street Pharmacy",
    "pcp-clinic": "Maple Family Medicine",
    "nc-csrs": "NC Controlled Substances Reporting System",
}
QUALIFYING_SPO2 = 88
# Maple Family Medicine is part of a multi-specialty group: follow-ups go to the right clinic.
CLINICIANS = {
    "primary care": ("Dr. Alex Lee", "Maple Family Medicine"),
    "cardiology": ("Dr. Maya Chen", "Maple Health Cardiology"),
    "nephrology": ("Dr. Omar Haddad", "Maple Health Kidney Clinic"),
    "neurology": ("Dr. Lena Park", "Maple Health Neurology"),
    "orthopedics": ("Dr. Sam Rivera", "Maple Health Orthopedics"),
    "endocrinology": ("Dr. Ana Souza", "Maple Health Diabetes Center"),
}
# The registry's fill history (everyone else: no controlled-substance fills).
REGISTRY_HISTORY = {
    "Jane Doe": {"fills_last_12_months": 11, "prescribers": 1, "pharmacies": 1,
                 "last_fill_days_ago": 28, "drug": "oxycodone 5 mg", "quantity": 20},
}


def create_app(validators: Callable[[str], JwtValidator], admin_key: str,
               today: Callable[[], date] = date.today) -> FastAPI:
    app = FastAPI(title="Mock partners", docs_url=None, redoc_url=None,
                  telemetry=FASTAPI_TELEMETRY)
    received: list[dict[str, Any]] = []

    def accept(request: Request, partner: str, body: dict[str, Any]) -> TokenClaims:
        """Check the token is addressed to this partner, then log what arrived."""
        header = request.headers.get("authorization", "")
        token = header.removeprefix("Bearer ").strip()
        try:
            claims = validators(partner).validate(token)
        except TokenValidationError as exc:
            raise HTTPException(401, detail=f"{PARTNER_NAMES[partner]} rejected the token: "
                                            f"{exc}") from exc
        received.append({"partner": partner, "partner_name": PARTNER_NAMES[partner],
                         "at": datetime.now().isoformat(timespec="seconds"),
                         "path": request.url.path, "sent_by": claims.authorized_party,
                         "on_behalf_of": claims.username, "fields": sorted(body),
                         "body": body})
        trace.get_current_span().set_attributes({
            "partner.name": partner, "partner.fields": ",".join(sorted(body)),
            "partner.sent_by": claims.authorized_party})
        return claims

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # ------------------------------------------------------------------ payer
    @app.post("/payer/eligibility")
    async def eligibility(request: Request) -> dict[str, Any]:
        body = await request.json()
        accept(request, "payer", body)
        return {"member_id": body.get("member_id"), "status": "active",
                "plan": "PPO Gold", "dme_benefit": "covered after prior authorization"}

    @app.post("/payer/prior-auth")
    async def prior_auth(request: Request) -> dict[str, Any]:
        body = await request.json()
        accept(request, "payer", body)
        evidence = " ".join(str(e) for e in body.get("evidence", []))
        readings = [int(v) for v in re.findall(r"(\d{2})\s*%", evidence)]
        qualifies = any(v <= QUALIFYING_SPO2 for v in readings)
        if "oxygen" in str(body.get("service", "")).lower() and qualifies:
            return {"status": "approved", "authorization": f"PA-{uuid.uuid4().hex[:6].upper()}",
                    "valid_days": 90, "note": "Qualifying exertional saturation documented"}
        return {"status": "pended", "note": f"Needs an oxygen saturation <= {QUALIFYING_SPO2}% "
                                            "on room air"}

    # ------------------------------------------------------------------ state registry
    @app.post("/nc-csrs/query")
    async def registry(request: Request) -> dict[str, Any]:
        body = await request.json()
        accept(request, "nc-csrs", body)
        history = REGISTRY_HISTORY.get(body.get("name", ""))
        if history is None:
            return {"patient": body.get("name"), "drug": body.get("drug"),
                    "fills_last_12_months": 0, "prescribers": 0, "pharmacies": 0,
                    "last_fill": None, "risk_flags": []}
        last = today() - timedelta(days=history["last_fill_days_ago"])
        return {"patient": body.get("name"), "drug": body.get("drug"),
                "fills_last_12_months": history["fills_last_12_months"],
                "prescribers": history["prescribers"], "pharmacies": history["pharmacies"],
                "last_fill": {"date": last.isoformat(), "drug": history["drug"],
                              "quantity": history["quantity"],
                              "prescriber": "Dr. Morgana Pendragon",
                              "pharmacy": "Main Street Pharmacy"},
                "risk_flags": []}

    # ------------------------------------------------------------------ retail pharmacy
    @app.post("/retail-pharmacy/prescriptions")
    async def prescription(request: Request) -> dict[str, Any]:
        body = await request.json()
        accept(request, "retail-pharmacy", body)
        signature = body.get("signature") or {}
        # Defense in depth: the pharmacy recomputes the fingerprint of what it received.
        if fingerprint(body.get("prescription")) != signature.get("fingerprint"):
            raise HTTPException(422, detail="prescription does not match its signature")
        return {"status": "received", "rx_number": f"RX-{uuid.uuid4().hex[:7].upper()}",
                "ready": "today after 4 pm"}

    # ------------------------------------------------------------------ family doctor
    @app.post("/pcp-clinic/appointments")
    async def appointment(request: Request) -> dict[str, Any]:
        body = await request.json()
        accept(request, "pcp-clinic", body)
        within = int(body.get("within_days", 7))
        specialty = str(body.get("specialty") or "primary care").lower()
        if specialty not in CLINICIANS:
            raise HTTPException(422, detail=f"no {specialty} clinic in the Maple Health group")
        clinician, location = CLINICIANS[specialty]
        slot = today() + timedelta(days=max(1, min(within - 1, 6)))
        return {"status": "booked", "clinician": clinician, "specialty": specialty,
                "when": f"{slot.isoformat()} 10:00", "location": location}

    @app.post("/pcp-clinic/documents")
    async def document(request: Request) -> dict[str, Any]:
        body = await request.json()
        accept(request, "pcp-clinic", body)
        return {"status": "filed", "document_id": f"DOC-{uuid.uuid4().hex[:6].upper()}"}

    # ------------------------------------------------------------------ admin
    def check_admin(key: str | None) -> None:
        if not admin_key or not secrets.compare_digest(key or "", admin_key):
            raise HTTPException(401, detail="demo admin key required")

    @app.get("/admin/received")
    def what_was_received(partner: str | None = None,
                          x_demo_admin_key: str | None = Header(default=None)) -> dict:
        check_admin(x_demo_admin_key)
        return {"received": [r for r in received if partner in (None, r["partner"])]}

    @app.delete("/admin/received", status_code=204)
    def forget(x_demo_admin_key: str | None = Header(default=None)) -> None:
        check_admin(x_demo_admin_key)
        received.clear()

    return app
