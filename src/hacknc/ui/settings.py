"""Settings specific to the UI service (on top of ``hacknc.common.config.Settings``)."""

from __future__ import annotations

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class UiSettings(BaseSettings):
    """Where the UI lives and how it talks to Keycloak and Grafana."""

    model_config = SettingsConfigDict(extra="ignore")

    ui_base_url: str = "http://localhost:8000"
    ui_client_id: str = "hospital-ui"
    ui_session_secret: SecretStr = SecretStr("change-me")
    # Browser-facing links (always localhost: they are opened by the presenter's browser).
    grafana_url: str = "http://localhost:3000"
    keycloak_admin_url: str = "http://localhost:8080/admin/master/console/#/hacknc-hospital"
    opa_url: str = "http://localhost:8181"
    # Back-channel URLs of our services (inside Docker these use service names).
    approval_url: str = "http://localhost:8007"
    audit_url: str = "http://localhost:8006"
    orchestrator_url: str = "http://localhost:8001"
    ehr_mcp_url: str = "http://localhost:8010/mcp"
    partners_url: str = "http://localhost:8020"
    # Shared with the approval service so the controls page can flip demo switches.
    demo_admin_key: str = ""

    @property
    def redirect_uri(self) -> str:
        return f"{self.ui_base_url.rstrip('/')}/auth/callback"
