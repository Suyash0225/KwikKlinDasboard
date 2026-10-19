"""Google OAuth for the vendor Control panel.

These tests exercise the browser flow without contacting Google's live endpoints.
The shared Google token exchange is covered separately in test_google_login.py.
"""

from urllib.parse import parse_qs, urlparse

import pytest

from app.config import settings
from app.services import google_auth

START = "/control/auth/google"
CALLBACK = "/control/auth/google/callback"


@pytest.fixture
def control_google_on(monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "control-client-id")
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "control-client-secret")
    monkeypatch.setattr(settings, "CONTROL_GOOGLE_ALLOWED_EMAILS", "Owner@Example.com, backup@example.com")
    monkeypatch.setattr(settings, "CONTROL_GOOGLE_BASE_URL", "https://kwikklin.test/control")
    monkeypatch.setattr(settings, "CONTROL_GOOGLE_LEVEL", "danger")
    calls = {}

    async def fake_exchange(code, base=None, callback_path="/api/auth/google/callback"):
        calls.update(code=code, base=base, callback_path=callback_path)
        if code != "good-code":
            raise google_auth.GoogleAuthError("Google ne login confirm nahi kiya")
        return {
            "sub": "google-control-sub",
            "email": "owner@example.com",
            "name": "Control Owner",
            "picture": "",
        }

    monkeypatch.setattr(google_auth, "exchange_code", fake_exchange)
    return calls


async def test_control_google_start_requires_credentials_and_allowlist(client, monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "")
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "")
    monkeypatch.setattr(settings, "CONTROL_GOOGLE_ALLOWED_EMAILS", "")

    response = await client.get(START, follow_redirects=False)
    assert response.status_code == 503


async def test_control_google_start_uses_exact_callback_and_sets_state(client, control_google_on):
    response = await client.get(START, follow_redirects=False)

    assert response.status_code == 302
    assert "accounts.google.com" in response.headers["location"]
    params = parse_qs(urlparse(response.headers["location"]).query)
    assert params["redirect_uri"] == ["https://kwikklin.test/control/auth/google/callback"]
    assert params["state"]
    assert google_auth.STATE_COOKIE in response.cookies
    assert "httponly" in response.headers["set-cookie"].lower()
    assert "max-age=600" in response.headers["set-cookie"].lower()


async def test_control_google_callback_rejects_csrf_state(client, control_google_on):
    response = await client.get(
        f"{CALLBACK}?code=good-code&state=attacker-state",
        follow_redirects=False,
    )
    assert response.status_code == 400


async def test_control_google_callback_allowlists_identity_and_mints_session(client, control_google_on):
    start = await client.get(START, follow_redirects=False)
    state = start.cookies[google_auth.STATE_COOKIE]
    client.cookies.set(google_auth.STATE_COOKIE, state, path="/control")

    response = await client.get(
        f"{CALLBACK}?code=good-code&state={state}",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/control"
    assert "kk_vendor" in response.cookies
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "path=/control" in cookie
    assert control_google_on == {
        "code": "good-code",
        "base": "https://kwikklin.test/control",
        "callback_path": "/auth/google/callback",
    }


async def test_control_google_callback_denies_non_allowlisted_account(client, control_google_on, monkeypatch):
    async def attacker_identity(code, base=None, callback_path="/api/auth/google/callback"):
        return {
            "sub": "attacker-sub",
            "email": "attacker@example.com",
            "name": "Attacker",
            "picture": "",
        }

    monkeypatch.setattr(google_auth, "exchange_code", attacker_identity)
    start = await client.get(START, follow_redirects=False)
    state = start.cookies[google_auth.STATE_COOKIE]
    client.cookies.set(google_auth.STATE_COOKIE, state, path="/control")

    response = await client.get(
        f"{CALLBACK}?code=good-code&state={state}",
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert "kk_vendor" not in response.cookies
