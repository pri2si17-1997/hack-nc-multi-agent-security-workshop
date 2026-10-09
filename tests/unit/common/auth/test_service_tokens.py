from hacknc.common.auth import TokenSet
from hacknc.common.auth.service_tokens import ServiceTokenProvider


class CountingKeycloak:
    def __init__(self):
        self.calls = []

    def client_credentials(self, client_id, secret, scopes):
        self.calls.append((client_id, list(scopes)))
        return TokenSet(access_token=f"token-{len(self.calls)}", expires_in=300)


def test_token_is_cached_until_near_expiry() -> None:
    kc, now = CountingKeycloak(), [0.0]
    provider = ServiceTokenProvider(kc, "ehr-mcp", "s", "audit-service", ["audit:write"],
                                    clock=lambda: now[0])
    assert provider.token() == "token-1"
    now[0] = 200
    assert provider.token() == "token-1"
    now[0] = 280  # within the 30 s renewal margin
    assert provider.token() == "token-2"
    assert kc.calls[0] == ("ehr-mcp", ["aud-audit-service", "audit:write"])
