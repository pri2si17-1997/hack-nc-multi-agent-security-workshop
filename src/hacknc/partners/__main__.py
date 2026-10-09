"""Run the mock partners: ``python -m hacknc.partners`` (port 8020)."""

from __future__ import annotations

import os

import jwt
import uvicorn

from hacknc.common.auth import JwtValidator, KeycloakClient
from hacknc.common.config import get_settings
from hacknc.common.telemetry import setup_tracing
from hacknc.partners.app import create_app


def main() -> None:
    settings = get_settings()
    setup_tracing("partners", settings.otel_exporter_otlp_endpoint)
    keys = jwt.PyJWKClient(KeycloakClient(settings.keycloak_url).jwks_uri, cache_keys=True)

    def validators(partner: str) -> JwtValidator:
        return JwtValidator(settings.keycloak_issuer, partner, key_provider=keys)

    app = create_app(validators, os.environ.get("DEMO_ADMIN_KEY", ""))
    uvicorn.run(app, host="0.0.0.0", port=8020)


if __name__ == "__main__":
    main()
