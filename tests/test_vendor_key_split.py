"""P4: vendor (platform) key aur dukaan (shop) key alag.

VENDOR_API_KEY set hone par ADMIN_API_KEY /control par nahi chalta, aur
vendor key dukaan ke dashboard par nahi chalta. Set na ho to purana
behaviour — ek hi key dono jagah (single-shop deploy)."""

import pytest

from app.config import settings

VENDOR = "vendor-only-key-for-tests-0001"


@pytest.fixture
def split_keys(monkeypatch):
    monkeypatch.setattr(settings, "VENDOR_API_KEY", VENDOR)
    yield
    monkeypatch.setattr(settings, "VENDOR_API_KEY", "")


async def test_shop_key_cannot_open_control_when_vendor_key_is_set(client, split_keys) -> None:
    r = await client.get("/control/api/tenants", headers={"X-API-Key": settings.ADMIN_API_KEY})
    assert r.status_code == 401
    r = await client.get("/control/api/tenants", headers={"X-API-Key": VENDOR})
    assert r.status_code == 200


async def test_vendor_key_cannot_open_the_shop_dashboard(client, split_keys) -> None:
    r = await client.get("/admin/api/rates", headers={"X-API-Key": VENDOR})
    assert r.status_code == 401
    r = await client.get("/admin/api/rates", headers={"X-API-Key": settings.ADMIN_API_KEY})
    assert r.status_code == 200


async def test_without_vendor_key_the_legacy_single_key_still_works(client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "VENDOR_API_KEY", "")
    r = await client.get("/control/api/tenants", headers={"X-API-Key": settings.ADMIN_API_KEY})
    assert r.status_code == 200


async def test_vendor_session_cookie_is_signed_with_the_vendor_key(client, split_keys) -> None:
    from app.routers.orders import mint_vendor_token, verify_vendor_token

    tok = mint_vendor_token("danger", "env-key")
    assert await verify_vendor_token(tok) is not None
    # key badli -> purani cookie bekaar (rotation kaam karti hai)
    settings.VENDOR_API_KEY = VENDOR + "-rotated"
    assert await verify_vendor_token(tok) is None
