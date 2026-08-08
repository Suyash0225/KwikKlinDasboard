"""Google se login.

Yahan galti ka matlab hai kisi ka account kisi aur ke haath lag jaana,
isliye tests un cheezon par hain jo asli hamle hain: CSRF state, dusre app
ka token, bina verify kiya email, aur form se manmana email bhar dena.
"""

import json

import pytest
from sqlalchemy import select, text as sqltext

from app.config import settings
from app.database import async_session_factory
from app.models import Tenant, User
from app.services import auth, google_auth

PHONE = "+919999900055"
GEMAIL = "google-shop@example.com"


@pytest.fixture(autouse=True)
async def _clean():
    async def wipe():
        async with async_session_factory() as s:
            await s.execute(
                sqltext(
                    "DELETE FROM login_sessions WHERE user_id IN (SELECT id FROM users "
                    "WHERE tenant_id IN (SELECT id FROM tenants WHERE owner_phone = :p))"
                ),
                {"p": PHONE},
            )
            await s.execute(
                sqltext(
                    "DELETE FROM users WHERE tenant_id IN "
                    "(SELECT id FROM tenants WHERE owner_phone = :p)"
                ),
                {"p": PHONE},
            )
            await s.execute(
                sqltext("DELETE FROM tenants WHERE owner_phone = :p"), {"p": PHONE}
            )
            await s.commit()

    await wipe()
    yield
    await wipe()


@pytest.fixture
def google_on(monkeypatch):
    """Google configured hai, aur token exchange humare haath mein hai."""
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "test-client-id.apps.googleusercontent.com")
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "test-secret")

    async def fake_exchange(code: str, base: str | None = None) -> dict:
        if code != "good-code":
            raise google_auth.GoogleAuthError("Google ne login confirm nahi kiya")
        return {"sub": "google-sub-123", "email": GEMAIL, "name": "Google Seth", "picture": ""}

    monkeypatch.setattr(google_auth, "exchange_code", fake_exchange)
    return True


# --- id_token ki jaanch (asli crypto logic) --------------------------------


def _id_token(claims: dict) -> str:
    import base64

    def seg(d):
        raw = json.dumps(d).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{seg({'alg': 'RS256'})}.{seg(claims)}.signature"


@pytest.fixture
def fake_google_token(monkeypatch):
    """Google ke token endpoint ki jagah humara jawab."""
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "my-app")
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "s")

    def _install(claims: dict):
        class FakeResp:
            status_code = 200

            def json(self):
                return {"id_token": _id_token(claims)}

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, *a, **kw):
                return FakeResp()

        monkeypatch.setattr(google_auth.httpx, "AsyncClient", lambda **kw: FakeClient())

    return _install


async def test_token_for_another_app_is_refused(fake_google_token) -> None:
    """Kisi aur app ka Google token humare yahan nahi chalega."""
    fake_google_token(
        {"aud": "someone-else", "iss": "https://accounts.google.com",
         "email": "x@example.com", "email_verified": True, "sub": "1"}
    )
    with pytest.raises(google_auth.GoogleAuthError):
        await google_auth.exchange_code("code")


async def test_unverified_email_is_refused(fake_google_token) -> None:
    """Bina verify kiye email par account dena = koi bhi kisi ka le le."""
    fake_google_token(
        {"aud": "my-app", "iss": "https://accounts.google.com",
         "email": "x@example.com", "email_verified": False, "sub": "1"}
    )
    with pytest.raises(google_auth.GoogleAuthError):
        await google_auth.exchange_code("code")


async def test_a_good_token_gives_the_identity(fake_google_token) -> None:
    fake_google_token(
        {"aud": "my-app", "iss": "https://accounts.google.com",
         "email": "Seth@Example.com", "email_verified": True, "sub": "42", "name": "Seth"}
    )
    ident = await google_auth.exchange_code("code")
    assert ident["email"] == "seth@example.com" and ident["sub"] == "42"


# --- poora rasta -----------------------------------------------------------


async def test_google_button_hidden_until_configured(client) -> None:
    d = (await client.get("/api/plans")).json()
    assert "google_login" in d


async def test_start_needs_configuration(client) -> None:
    r = await client.get("/api/auth/google/start", follow_redirects=False)
    assert r.status_code == 503


async def test_start_sets_a_state_cookie(client, google_on) -> None:
    r = await client.get("/api/auth/google/start", follow_redirects=False)
    assert r.status_code == 302
    assert "accounts.google.com" in r.headers["location"]
    assert google_auth.STATE_COOKIE in r.cookies


async def test_callback_without_matching_state_is_refused(client, google_on) -> None:
    """CSRF: bina cookie ke aaya callback kisi hamlavar ka ho sakta hai."""
    r = await client.get(
        "/api/auth/google/callback?code=good-code&state=nakli", follow_redirects=False
    )
    assert r.status_code == 303
    assert "err=" in r.headers["location"]
    async with async_session_factory() as s:
        n = (
            await s.execute(sqltext("SELECT count(*) FROM users WHERE email = :e"),
                            {"e": GEMAIL})
        ).scalar_one()
    assert n == 0, "state galat tha, koi user nahi banna chahiye"


async def _callback(client, google_on) -> None:
    start = await client.get("/api/auth/google/start", follow_redirects=False)
    state = start.cookies[google_auth.STATE_COOKIE]
    client.cookies.set(google_auth.STATE_COOKIE, state)
    return await client.get(
        f"/api/auth/google/callback?code=good-code&state={state}", follow_redirects=False
    )


async def test_new_google_user_is_asked_for_shop_details(client, google_on) -> None:
    """Google naam-email deta hai, dukaan ka naam nahi — wo poochna padta hai."""
    r = await _callback(client, google_on)
    assert r.status_code == 303 and "google=1" in r.headers["location"]

    pend = (await client.get("/api/auth/google/pending")).json()
    assert pend["pending"] is True and pend["email"] == GEMAIL

    made = await client.post(
        "/api/signup/google",
        json={"shop_name": "Google Wali Laundry", "owner_name": "Google Seth",
              "phone": PHONE, "city": "Varanasi", "plan": "starter"},
    )
    assert made.status_code == 201, made.text
    assert made.json()["next_url"] in ("/admin", "/welcome")

    async with async_session_factory() as s:
        u = (await s.execute(select(User).where(User.email == GEMAIL))).scalar_one()
    assert u.auth_provider == "google" and u.google_sub == "google-sub-123"
    assert not auth.verify_password("", u.password_hash), "Google user ka password nahi chalta"


async def test_second_time_google_logs_straight_in(client, google_on) -> None:
    await _callback(client, google_on)
    await client.post(
        "/api/signup/google",
        json={"shop_name": "Google Wali Laundry", "owner_name": "Google Seth",
              "phone": PHONE, "plan": "starter"},
    )
    await client.post("/api/logout")
    client.cookies.clear()

    r = await _callback(client, google_on)
    assert r.status_code == 303
    assert r.headers["location"] in ("/admin", "/welcome")
    assert auth.SESSION_COOKIE in r.cookies, "seedha login ho jana chahiye"


async def test_google_cannot_claim_someone_elses_email(client, google_on) -> None:
    """Form ka email nazarandaz hota hai — email sirf Google se aata hai."""
    await _callback(client, google_on)
    r = await client.post(
        "/api/signup/google",
        json={"shop_name": "Chori Ki Dukaan", "owner_name": "Chor",
              "phone": PHONE, "plan": "starter",
              "email": "victim@example.com"},   # ignore hona chahiye
    )
    assert r.status_code == 201
    async with async_session_factory() as s:
        t = (
            await s.execute(select(Tenant).where(Tenant.owner_phone == PHONE))
        ).scalar_one()
    assert t.owner_email == GEMAIL, "email Google wala hi rehna chahiye"


async def test_signup_google_without_the_pending_cookie_is_refused(client, google_on) -> None:
    r = await client.post(
        "/api/signup/google",
        json={"shop_name": "Bina Google", "owner_name": "Koi", "phone": PHONE},
    )
    assert r.status_code == 400
