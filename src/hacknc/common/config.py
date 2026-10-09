"""Settings shared by every service, read from environment variables (and `.env`).

Each service reads the same handful of values: where Keycloak lives, how to
reach the OpenTelemetry Collector, and its own client secret.

Two Keycloak URLs exist on purpose:

* ``keycloak_issuer`` is the issuer printed inside every token (``iss`` claim).
  It is always ``http://localhost:8080/...`` because that is the public URL.
* ``keycloak_url`` is where *this* process sends HTTP requests. On the Mac it is
  the same as the issuer; inside a container it is ``http://keycloak:8080/...``.
"""

from __future__ import annotations

import os
from functools import lru_cache

from dotenv import load_dotenv
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_REALM_URL = "http://localhost:8080/realms/hacknc-hospital"


class Settings(BaseSettings):
    """Typed view of the environment. Field names map to upper-case env vars."""

    model_config = SettingsConfigDict(extra="ignore")

    keycloak_issuer: str = DEFAULT_REALM_URL
    keycloak_url: str = DEFAULT_REALM_URL
    demo_user_password: SecretStr | None = None
    otel_exporter_otlp_endpoint: str = "http://localhost:4318"


class MissingSecretError(RuntimeError):
    """Raised when a client secret is needed but not configured."""


def client_secret_env_var(client_id: str) -> str:
    """Return the env var that holds a client's secret.

    >>> client_secret_env_var("summary-agent")
    'SUMMARY_AGENT_CLIENT_SECRET'
    """
    return f"{client_id.upper().replace('-', '_')}_CLIENT_SECRET"


def client_secret(client_id: str) -> str:
    """Look up the Keycloak client secret for ``client_id`` from the environment."""
    var = client_secret_env_var(client_id)
    value = os.environ.get(var)
    if not value:
        raise MissingSecretError(f"{var} is not set (copy .env.example to .env)")
    return value


@lru_cache
def get_settings() -> Settings:
    """Load settings once per process. Values from ``.env`` never override real env vars."""
    load_dotenv(override=False)
    return Settings()
