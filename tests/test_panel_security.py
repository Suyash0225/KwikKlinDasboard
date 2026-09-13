"""Panel security hardening proof:
- master key browser storage mein nahi jaata: ek baar bhejo -> httpOnly,
  SameSite=Strict, path-scoped cookie
- cookie se level/permissions bilkul wahi (read key ab bhi read-only)
- per-admin key REVOKE hote hi uski cookie bhi mar jaati hai
- /control par strict CSP (script-src 'self', no unsafe-inline) aur
  shell mein koi inline script/handler nahi
"""

import re

import pytest
from sqlalchemy import text as sqltext

from app.config import settings
from app.database import async_session_factory
from app.routers.orders import VENDOR_COOKIE

# /control ka master key. VENDOR_API_KEY set ho to ADMIN_API_KEY wahan
# chalta hi NAHI (orders.vendor_master_key ka jaan-boojh kar rakha gaya
# niyam). Ye test seedha ADMIN_API_KEY bhejte the, isliye purane
# ek-dukaan wale .env par pass hote the aur alag vendor key wale par
# 401. Wahi helper use karo jo server use karta hai — dono soorat mein
# sahi.
from app.routers.orders import vendor_master_key

AUTH = {"X-API-Key": vendor_master_key()}


@pytest.fixture(autouse=True)
async def _cleanup():
    yield
    async with async_session_factory() as db:
        await db.execute(sqltext("DELETE FROM admin_keys WHERE label LIKE 'test-sec-%'"))
        await db.commit()


async def test_key_exchanges_for_httponly_cookie(client) -> None:
    r = await client.post("/control/api/session", headers=AUTH)
    assert r.status_code == 200 and r.json()["level"] == "danger"
    raw = r.headers["set-cookie"]
    low = raw.lower()
    assert VENDOR_COOKIE in raw
    assert "httponly" in low, "JS ko cookie padhne nahi deni (XSS defence)"
    assert "samesite=strict" in low, "CSRF defence"
    assert "path=/control" in low, "cookie baaki app ko kabhi na jaaye"
    # cookie ke bharose (bina key header ke) API chalti hai
    assert (await client.get("/control/api/tenants")).status_code == 200
    # aur cookie mein master key NAHI hai (signed token hai)
    assert settings.ADMIN_API_KEY not in raw

    r = await client.post("/control/api/session/logout")
    assert r.status_code == 200
    client.cookies.delete(VENDOR_COOKIE)
    assert (await client.get("/control/api/tenants")).status_code == 401


async def test_cookie_keeps_the_key_level(client) -> None:
    """read-level key ki cookie se bhi koi mutation nahi ho sakti."""
    r = await client.post("/control/api/admin-keys", headers=AUTH,
                          json={"label": "test-sec-read", "level": "read"})
    read_key = r.json()["key"]
    client.cookies.clear()
    r = await client.post("/control/api/session", headers={"X-API-Key": read_key})
    assert r.status_code == 200 and r.json()["level"] == "read"
    assert (await client.get("/control/api/tenants")).status_code == 200
    r = await client.patch("/control/api/tenants/kwik-klin", json={"notes": "x"})
    assert r.status_code == 403, "read cookie se write nahi hona chahiye"
    client.cookies.clear()


async def test_revoking_a_key_kills_its_cookie(client) -> None:
    r = await client.post("/control/api/admin-keys", headers=AUTH,
                          json={"label": "test-sec-rot", "level": "write"})
    key = r.json()["key"]
    keys = (await client.get("/control/api/admin-keys", headers=AUTH)).json()
    kid = next(k["id"] for k in keys if k["label"] == "test-sec-rot")
    client.cookies.clear()
    await client.post("/control/api/session", headers={"X-API-Key": key})
    assert (await client.get("/control/api/tenants")).status_code == 200
    # revoke (master key se) -> purani cookie turant bekaar
    await client.post(f"/control/api/admin-keys/{kid}/revoke", headers=AUTH)
    assert (await client.get("/control/api/tenants")).status_code == 401
    client.cookies.clear()


async def test_tampered_cookie_is_rejected(client) -> None:
    r = await client.post("/control/api/session", headers=AUTH)
    tok = client.cookies.get(VENDOR_COOKIE)
    client.cookies.set(VENDOR_COOKIE, tok[:-4] + "beef")   # signature toot gayi
    assert (await client.get("/control/api/tenants")).status_code == 401
    client.cookies.clear()


async def test_control_page_has_strict_csp_and_no_inline_js(client) -> None:
    r = await client.get("/control")
    assert r.status_code == 200
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp
    assert "frame-ancestors 'none'" in csp and "base-uri 'none'" in csp
    html = r.text
    assert "<script src=" in html
    # koi inline <script> body, koi on*= handler, koi style="" attribute
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>\s*\S", html), "inline script mila"
    assert not re.search(r"\son[a-z]+\s*=", html), "inline event handler mila"
    assert not re.search(r'\sstyle\s*=\s*"', html), "inline style attribute mila"
    assert 'lang="en"' in html and 'name="description"' in html
    assert 'autocomplete="new-password"' in html, "key input browser mein save na ho"
    assert 'class="skip"' in html and 'aria-live="polite"' in html


async def test_static_assets_are_cacheable(client) -> None:
    for path in ("/admin/static/control.js", "/admin/static/control.css"):
        r = await client.get(path)
        assert r.status_code == 200
        assert "max-age=31536000" in r.headers.get("cache-control", "")
    # panel JS mein master key kahin persist nahi hoti
    js = (await client.get("/admin/static/control.js")).text
    assert "localStorage" not in js, "panel ab web storage use hi nahi karta"


async def test_logged_out_requests_do_not_trip_the_throttle(client) -> None:
    """Sign-in page ka pehla load (koi cookie/key nahi) attack NAHI hai.

    Ye counter dashboard ke saath share hota hai — isliye ise ginne se
    panel AUR dukaan ka dashboard dono 429 mein chale jaate the.
    """
    from app.routers.orders import _AUTH_MAX_FAILURES, _FAILED_AUTH

    _FAILED_AUTH.clear()
    client.cookies.clear()
    for _ in range(_AUTH_MAX_FAILURES + 4):
        assert (await client.get("/control/api/session")).status_code == 401
    assert not any(_FAILED_AUTH.values()), "logged-out requests throttle mein gine gaye"
    # asli key ab bhi chalti hai (429 nahi)
    assert (await client.post("/control/api/session", headers=AUTH)).status_code == 200
    client.cookies.clear()

    # GALAT key zaroor ginni chahiye — brute force par lock lagta rahe
    _FAILED_AUTH.clear()
    bad = {"X-API-Key": "definitely-not-the-key"}
    for _ in range(_AUTH_MAX_FAILURES):
        await client.get("/control/api/tenants", headers=bad)
    r = await client.get("/control/api/tenants", headers=AUTH)
    assert r.status_code == 429, "brute-force ke baad lock lagna hi chahiye"
    _FAILED_AUTH.clear()


async def test_signed_out_panel_hides_data_sections(client) -> None:
    """Sign-in se pehle khali tables nahi — ek saaf 'sign in' state."""
    html = (await client.get("/control")).text
    assert 'id="signedout"' in html and "Sign in to see your clients" in html
    assert 'id="main" class="hidden"' in html, "data sections shuru mein chhupi rahein"


async def test_password_fields_have_a_show_hide_eye(client) -> None:
    """Login, signup aur set-password — teeno jagah aankh ka button."""
    for path in ("/join", "/invite/dummy-token"):
        html = (await client.get(path)).text
        assert ".pw-wrap" in html and 'class = "eye"' not in html
        assert "Show password" in html, f"{path}: eye toggle missing"
    js = (await client.get("/admin/static/control.js")).text
    assert "addEyeToggle" in js, "panel ke password/key field par bhi eye chahiye"
