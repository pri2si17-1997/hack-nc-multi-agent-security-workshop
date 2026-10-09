import pytest

from hacknc.common import config


def test_client_secret_env_var_naming() -> None:
    assert config.client_secret_env_var("summary-agent") == "SUMMARY_AGENT_CLIENT_SECRET"
    assert config.client_secret_env_var("ehr-mcp") == "EHR_MCP_CLIENT_SECRET"


def test_client_secret_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCHESTRATOR_CLIENT_SECRET", "s3cret")
    assert config.client_secret("orchestrator") == "s3cret"


def test_client_secret_missing_raises_helpful_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOBODY_CLIENT_SECRET", raising=False)
    with pytest.raises(config.MissingSecretError, match="NOBODY_CLIENT_SECRET"):
        config.client_secret("nobody")


def test_settings_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KEYCLOAK_URL", "http://keycloak:8080/realms/x")
    monkeypatch.setenv("DEMO_USER_PASSWORD", "pw")
    settings = config.Settings()
    assert settings.keycloak_url == "http://keycloak:8080/realms/x"
    assert settings.demo_user_password is not None
    assert settings.demo_user_password.get_secret_value() == "pw"


def test_settings_defaults_point_at_local_keycloak(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("KEYCLOAK_URL", "KEYCLOAK_ISSUER"):
        monkeypatch.delenv(var, raising=False)
    settings = config.Settings()
    assert settings.keycloak_issuer == config.DEFAULT_REALM_URL
    assert settings.keycloak_url == config.DEFAULT_REALM_URL
