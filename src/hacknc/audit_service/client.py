"""Read-side client for the audit service (the UI's audit page)."""

from __future__ import annotations

import httpx

from hacknc.common.http_api import ApiResult, call


class AuditApi:
    def __init__(self, base_url: str, http: httpx.Client | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._http = http or httpx.Client(timeout=10.0)

    def events(self, token: str, limit: int = 100, **filters: str | int | None) -> ApiResult:
        params = {"limit": limit, **{k: v for k, v in filters.items() if v is not None
                                     and v != ""}}
        return call(self._http, "GET", f"{self.base_url}/events", token, params=params)

    def verify(self, token: str) -> ApiResult:
        return call(self._http, "GET", f"{self.base_url}/verify", token)
