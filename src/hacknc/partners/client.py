"""Client for the mock partners (used only by the disclosure gateway and the admin console)."""

from __future__ import annotations

from typing import Any

import httpx

from hacknc.common.http_api import ApiResult, call

ENDPOINTS = {
    "eligibility": ("payer", "/payer/eligibility"),
    "prior_authorization": ("payer", "/payer/prior-auth"),
    "registry_query": ("nc-csrs", "/nc-csrs/query"),
    "prescription": ("retail-pharmacy", "/retail-pharmacy/prescriptions"),
    "appointment": ("pcp-clinic", "/pcp-clinic/appointments"),
    "care_transition": ("pcp-clinic", "/pcp-clinic/documents"),
}


class PartnerApi:
    def __init__(self, base_url: str, http: httpx.Client | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._http = http or httpx.Client(timeout=10.0)

    def send(self, purpose: str, token: str, payload: dict[str, Any]) -> ApiResult:
        _, path = ENDPOINTS[purpose]
        return call(self._http, "POST", f"{self.base_url}{path}", token, json=payload)

    def received(self, admin_key: str, partner: str | None = None) -> ApiResult:
        return call(self._http, "GET", f"{self.base_url}/admin/received",
                    headers={"X-Demo-Admin-Key": admin_key},
                    params={"partner": partner} if partner else None)

    def reset(self, admin_key: str) -> ApiResult:
        return call(self._http, "DELETE", f"{self.base_url}/admin/received",
                    headers={"X-Demo-Admin-Key": admin_key})
