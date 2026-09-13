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


async def test_all_four_checks_come_back(client, vendor) -> None:
    r = await client.get(URL, headers=vendor)

    assert r.status_code == 200
    checks = _by_key(r.json())
    assert set(checks) == {"creds", "secret", "subscription", "inbound"}
    for c in checks.values():
        assert c["label"] and c["detail"], "har check ka naam aur wajah dono chahiye"


async def test_missing_token_is_named_not_just_failed(client, vendor, monkeypatch) -> None:
    monkeypatch.setattr(settings, "WHATSAPP_TOKEN", "")

    checks = _by_key((await client.get(URL, headers=vendor)).json())

    assert checks["creds"]["ok"] is False
    assert ".env" in checks["creds"]["detail"], "batao kahan bharna hai"


async def test_empty_app_secret_is_called_out(client, vendor, monkeypatch) -> None:
    """Iske bina bhejna theek chalta hai — sirf aana band hota hai."""
    monkeypatch.setattr(settings, "WHATSAPP_APP_SECRET", "")

    checks = _by_key((await client.get(URL, headers=vendor)).json())

    assert checks["secret"]["ok"] is False
    assert "403" in checks["secret"]["detail"], "wajah batao, sirf 'galat' mat kaho"


async def test_a_present_secret_is_unknown_not_green(client, vendor, monkeypatch) -> None:
    """Secret sirf ek asli signed webhook hi sabit karta hai.

    Bhara hua dekh kar hara dikhana jhooth hai — galat secret bhi bhara
    hua hi dikhta hai, aur yahi wo halat hai jise ye panel pakadna chahta
    hai.
    """
    monkeypatch.setattr(settings, "WHATSAPP_APP_SECRET", "x" * 32)

    checks = _by_key((await client.get(URL, headers=vendor)).json())

    assert checks["secret"]["ok"] is None


async def test_no_inbound_ever_is_a_failure(client, vendor) -> None:
    """Test DB mein koi inbound nahi — yahi wo halat hai jo pakadni thi."""
    checks = _by_key((await client.get(URL, headers=vendor)).json())

    assert checks["inbound"]["ok"] is False
    assert r"ek bhi nahi" in checks["inbound"]["detail"]


async def test_subscription_is_unknown_without_app_id(client, vendor, monkeypatch) -> None:
    """APP_ID ke bina check ho hi nahi sakta — usko 'fail' kehna jhooth hai."""
    monkeypatch.setattr(settings, "WHATSAPP_APP_ID", "")

    checks = _by_key((await client.get(URL, headers=vendor)).json())

    assert checks["subscription"]["ok"] is None


async def test_unknown_does_not_make_the_whole_thing_red(client, vendor, monkeypatch) -> None:
    """`ok` sirf pakke fail par False ho. 'Pata nahi' alarm nahi hai —
    warna panel hamesha laal rahega aur log dekhna chhod denge."""
    monkeypatch.setattr(settings, "WHATSAPP_APP_ID", "")
    monkeypatch.setattr(settings, "WHATSAPP_APP_SECRET", "x" * 32)
    body = (await client.get(URL, headers=vendor)).json()

    unknowns = [c for c in body["checks"] if c["ok"] is None]
    fails = [c for c in body["checks"] if c["ok"] is False]

    assert unknowns, "is halat mein kuch 'pata nahi' hona chahiye"
    assert body["ok"] is (not fails)


async def test_the_endpoint_is_vendor_only(client) -> None:
    assert (await client.get(URL)).status_code in (401, 403)
