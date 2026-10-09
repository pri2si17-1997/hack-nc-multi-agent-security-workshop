"""Tiny helper for calling our own JSON services and keeping their answer, even on 4xx."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass(frozen=True)
class ApiResult:
    """Status code + JSON body. Callers show ``reason`` to the user when ``ok`` is False."""

    status: int
    body: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def reason(self) -> str:
        return str(self.body.get("reason") or self.body.get("detail") or f"HTTP {self.status}")


def call(http: httpx.Client, method: str, url: str, token: str | None = None,
         **kwargs: Any) -> ApiResult:
    headers = kwargs.pop("headers", {})
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        response = http.request(method, url, headers=headers, **kwargs)
    except httpx.HTTPError as exc:
        return ApiResult(503, {"reason": f"service unreachable ({exc.__class__.__name__})"})
    try:
        body = response.json() if response.content else {}
    except ValueError:
        body = {"reason": response.text[:200]}
    return ApiResult(response.status_code, body if isinstance(body, dict) else {"data": body})
