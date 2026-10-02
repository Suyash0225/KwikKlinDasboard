"""/control/api/whatsapp/health — chaar check, aur kyun chaaron chahiye.

Per-tenant creds jodte waqt live validate hote hain. Home dukaan ke creds
.env se aate hain aur unka koi check tha hi nahi.

Teen alag kharabiyan bahar se ek jaisi dikhti hain — "bot jawab nahi de
raha" — aur inka ilaaj alag-alag hai:

  token expire       -> bhejna band, aana chalu
  APP_SECRET galat   -> bhejna chalu, aana band (403)
  messages subscribe -> dono chup, Meta verification phir bhi pass

Isliye ye endpoint chaaron alag-alag batata hai, ek "sab theek/kharab"
nahi. Aakhri check — aakhri INBOUND message — asli saboot hai: upar sab
hara aur wo khali, matlab gadbad aane wale raste mein hai.
"""

import pytest

from app.config import settings
from app.routers.orders import vendor_master_key

URL = "/control/api/whatsapp/health"


@pytest.fixture
def vendor():
    """/control ka master key — VENDOR_API_KEY set ho to ADMIN_API_KEY
    yahan chalta hi nahi, isliye helper se hi lo."""
    return {"X-API-Key": vendor_master_key()}


@pytest.fixture(autouse=True)
def no_real_graph_calls(monkeypatch):
    """Test kabhi Meta ko sach mein na poochhe."""
    async def ok(pnid, token):
        return True

    monkeypatch.setattr("app.services.whatsapp.validate_credentials", ok)


def _by_key(body: dict) -> dict:
    return {c["key"]: c for c in body["checks"]}


async def test_waha_health_checks_come_back(client, vendor, monkeypatch) -> None:
    monkeypatch.setattr(settings, "WHATSAPP_PROVIDER", "waha")
    r = await client.get(URL, headers=vendor)

    assert r.status_code == 200
    body = r.json()
    checks = _by_key(body)
    assert set(checks) == {"provider", "waha", "inbound"}
    assert body["provider"] == "waha"
    for c in checks.values():
        assert c["label"] and c["detail"], "har check ka naam aur wajah dono chahiye"


async def test_provider_check_reports_non_waha_configuration(client, vendor, monkeypatch) -> None:
    monkeypatch.setattr(settings, "WHATSAPP_PROVIDER", "meta")

    checks = _by_key((await client.get(URL, headers=vendor)).json())

    assert checks["provider"]["ok"] is False
    assert "meta" in checks["provider"]["detail"]


async def test_waha_configuration_check_uses_base_url(client, vendor, monkeypatch) -> None:
    monkeypatch.setattr(settings, "WAHA_BASE_URL", "")

    checks = _by_key((await client.get(URL, headers=vendor)).json())

    assert checks["waha"]["ok"] is False
    assert "WAHA_BASE_URL" in checks["waha"]["detail"]


async def test_inbound_check_is_explicit(client, vendor) -> None:
    checks = _by_key((await client.get(URL, headers=vendor)).json())

    assert isinstance(checks["inbound"]["ok"], bool)
    assert checks["inbound"]["label"]
    assert checks["inbound"]["detail"]


async def test_the_endpoint_is_vendor_only(client) -> None:
    assert (await client.get(URL)).status_code in (401, 403)
