"""Control panel Google OAuth tests."""

import pytest

from app.config import settings
from app.services import google_auth

ALLOWED = "suyashsrivstava49@gmail.com"


@pytest.fixture
def control_google_on(monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "control-test-client")
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "control-test-secret")
    monkeypatch.setattr(settings, "CONTROL_GOOGLE_ALLOWED_EMAILS", ALLOWED)
    monkeypatch.setattr(settings, "CONTROL_GOOGLE_LEVEL", "danger")
    monkeypatch.setattr(settings, "CONTROL_GOOGLE_BASE_URL", "https://kwikklin.online/control")

    async def fake_exchange(code: str, base: str | None = None) -> dict:
        assert base == "https://kwikklin.online/control"
        if code == "allowed":
            return {
                "sub": "google-sub-control",
                "email": ALLOWED,
                "name": "Suyash",
                "picture": "",
            }
        if code == "other":
            return {
                "sub": "google-sub-other",
                "email": "other@example.com",
                "name": "Other",
                "picture": "",
            }
        raise google_auth.GoogleAuthError("bad code")

    monkeypatch.setattr(google_auth, "exchange_code", fake_exchange)


async def test_control_google_start_is_public_and_sets_state(client, control_google_on):
    r = await client.get("/control/auth/google", follow_redirects=False)
    assert r.status_code == 302
    assert "accounts.google.com" in r.headers["location"]
    assert google_auth.STATE_COOKIE in r.cookies


async def test_control_google_rejects_bad_state(client, control_google_on):
    r = await client.get(
        "/control/auth/google/callback?code=allowed&state=wrong",
        follow_redirects=False,
    )
    assert r.status_code == 400


async def test_control_google_rejects_unallowed_account(client, control_google_on):
    start = await client.get("/control/auth/google", follow_redirects=False)
    state = start.cookies[google_auth.STATE_COOKIE]
    client.cookies.set(google_auth.STATE_COOKIE, state)

    r = await client.get(
        f"/control/auth/google/callback?code=other&state={state}",
        follow_redirects=False,
    )
    assert r.status_code == 403


async def test_control_google_mints_vendor_session(client, control_google_on):
    start = await client.get("/control/auth/google", follow_redirects=False)
    state = start.cookies[google_auth.STATE_COOKIE]
    client.cookies.set(google_auth.STATE_COOKIE, state)

    r = await client.get(
        f"/control/auth/google/callback?code=allowed&state={state}",
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/control"

    # The normal vendor session is now enough for protected Control APIs.
    r2 = await client.get("/control/api/session")
    assert r2.status_code == 200
    assert r2.json()["level"] == "danger"
    assert r2.json()["label"] == "Suyash"
