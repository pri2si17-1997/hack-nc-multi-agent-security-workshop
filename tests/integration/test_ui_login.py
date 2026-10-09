"""Logs into the running UI through Keycloak's real login form, like a browser would.

Needs `make up`. Run with: make test-integration
"""

from __future__ import annotations

import html
import re
from urllib.parse import urljoin

import httpx
import pytest

from hacknc.common.config import get_settings

pytestmark = pytest.mark.integration

UI = "http://localhost:8000"
FORM_ACTION = re.compile(r'<form[^>]*id="kc-form-login"[^>]*action="([^"]+)"', re.S)


@pytest.fixture
def browser():
    try:
        httpx.get(f"{UI}/healthz", timeout=3).raise_for_status()
    except httpx.HTTPError:
        pytest.skip("UI is not reachable; run `make up` first")
    with httpx.Client(follow_redirects=False, timeout=10) as client:
        yield client


def secure_cookies(response: httpx.Response) -> str:
    """Keycloak's login cookies are marked ``Secure``. Browsers still send them to
    http://localhost (a "potentially trustworthy" origin); httpx does not, so we do it."""
    pairs = [h.split(";", 1)[0] for h in response.headers.get_list("set-cookie")]
    return "; ".join(pairs)


def login_as(browser: httpx.Client, username: str) -> None:
    """Follow the full Authorization Code + PKCE flow through Keycloak's login page."""
    to_keycloak = browser.get(f"{UI}/login", params={"persona": username})
    login_page = browser.get(to_keycloak.headers["location"])
    assert login_page.status_code == 200, login_page.text[:300]
    action = html.unescape(FORM_ACTION.search(login_page.text).group(1))

    password = get_settings().demo_user_password.get_secret_value()
    back_to_ui = browser.post(urljoin(str(login_page.url), action),
                              data={"username": username, "password": password},
                              headers={"Cookie": secure_cookies(login_page)})
    assert back_to_ui.status_code == 302, back_to_ui.text[:300]
    assert back_to_ui.headers["location"].startswith(f"{UI}/auth/callback")

    landed = browser.get(back_to_ui.headers["location"])
    assert landed.headers["location"] in ("/ward", "/admin", "/portal"), landed.text[:300]


def test_physician_lands_on_the_ward(browser) -> None:
    login_as(browser, "dr.morgana")
    page = browser.get(f"{UI}/ward")
    assert page.status_code == 200
    assert "Jane Doe" in page.text and "Hobert Lynch" in page.text and "4 West (12)" in \
        page.text.replace('<span class="muted" id="ward-count">', "").replace("</span>", "")


def test_admin_lands_on_the_console(browser) -> None:
    login_as(browser, "admin.percival")
    assert browser.get(f"{UI}/admin").status_code == 200
    assert browser.get(f"{UI}/ward").headers["location"] == "/admin"


def test_logout_ends_the_session(browser) -> None:
    login_as(browser, "nurse.gawain")
    logout = browser.get(f"{UI}/logout")
    assert "/protocol/openid-connect/logout" in logout.headers["location"]
    assert browser.get(f"{UI}/ward").status_code == 303
