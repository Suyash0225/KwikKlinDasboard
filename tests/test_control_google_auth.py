"""Control Room login regression tests.

The supported browser login is the configured login ID/password exchange at
/control/api/session. Google OAuth tests belonged to an unimplemented route
and no longer described the application's authentication contract.
"""

import pytest

from app.config import settings

SESSION = "/control/api/session"


@pytest.fixture
def control_login_on(monkeypatch):
    monkeypatch.setattr(settings, "CONTROL_LOGIN_ID", "owner@example.com")
    monkeypatch.setattr(settings, "CONTROL_LOGIN_PASSWORD", "test-only-password-123")


async def test_control_login_accepts_configured_credentials(client, control_login_on):
    response = await client.post(
        SESSION,
        json={"login": "owner@example.com", "password": "test-only-password-123"},
    )

    assert response.status_code == 200
    assert response.json()["level"] == "danger"
    assert response.json()["label"] == "owner@example.com"
    assert "kk_vendor" in response.cookies

    session = await client.get(SESSION)
    assert session.status_code == 200
    assert session.json()["level"] == "danger"
    assert session.json()["label"] == "owner@example.com"


async def test_control_login_rejects_invalid_credentials(client, control_login_on):
    response = await client.post(
        SESSION,
        json={"login": "attacker@example.com", "password": "wrong-password-123"},
    )

    assert response.status_code == 401
