"""Client for the approval service (used by the hospital app and the two gateways)."""

from __future__ import annotations

from typing import Any

import httpx

from hacknc.common.http_api import ApiResult, call


class ApprovalApi:
    def __init__(self, base_url: str, http: httpx.Client | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._http = http or httpx.Client(timeout=10.0)

    def create(self, token: str, body: dict[str, Any]) -> ApiResult:
        return call(self._http, "POST", f"{self.base_url}/requests", token, json=body)

    def decide(self, token: str, request_id: str, approve: bool, note: str = "") -> ApiResult:
        verb = "approve" if approve else "reject"
        return call(self._http, "POST", f"{self.base_url}/requests/{request_id}/{verb}", token,
                    json={"note": note})

    def consume(self, token: str, request_id: str, content: dict[str, Any],
                patient_id: str, kind: str) -> ApiResult:
        return call(self._http, "POST", f"{self.base_url}/requests/{request_id}/consume", token,
                    json={"content": content, "patient_id": patient_id, "kind": kind})

    def break_glass(self, token: str, patient_id: str, justification: str) -> ApiResult:
        return call(self._http, "POST", f"{self.base_url}/break-glass", token,
                    json={"patient_id": patient_id, "justification": justification})

    def grants(self, token: str) -> ApiResult:
        return call(self._http, "GET", f"{self.base_url}/break-glass/grants", token)

    def get_flags(self, admin_key: str) -> ApiResult:
        return call(self._http, "GET", f"{self.base_url}/admin/flags",
                    headers={"X-Demo-Admin-Key": admin_key})

    def set_flags(self, admin_key: str, flags: dict[str, bool]) -> ApiResult:
        """Change demo switches, e.g. ``set_flags(key, {"scenario.oversharing": True})``."""
        return call(self._http, "PUT", f"{self.base_url}/admin/flags",
                    headers={"X-Demo-Admin-Key": admin_key}, json=flags)

    def reset(self, admin_key: str) -> ApiResult:
        return call(self._http, "DELETE", f"{self.base_url}/admin/requests",
                    headers={"X-Demo-Admin-Key": admin_key})
