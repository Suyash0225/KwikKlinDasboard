"""Inbox tools: template send, new chat, bulk customer import."""

import pytest
from sqlalchemy import select, text as sqltext

from app.config import settings
from app.database import async_session_factory
from app.models import Customer

AUTH = {"X-API-Key": settings.ADMIN_API_KEY}
P1, P2, P3 = "+919999900611", "+919999900612", "+919999900613"


@pytest.fixture(autouse=True)
async def _cleanup():
    yield
    from tests.conftest import purge_phones

    await purge_phones(P1, P2, P3)


async def test_new_chat_creates_thread(client) -> None:
    r = await client.post("/admin/api/inbox/new-chat", headers=AUTH,
                          json={"phone": "99999 00611", "name": "Naya Grahak"})
    assert r.status_code == 200 and r.json()["phone"] == P1
    async with async_session_factory() as s:
        cust = (await s.execute(select(Customer).where(Customer.phone == P1))).scalar_one()
        assert cust.name == "Naya Grahak"


async def test_send_template_to_new_number(client, monkeypatch) -> None:
    import app.routers.admin as admin_module

    calls = []

    async def fake_send(db, *, to_phone, template_name=None, template_params=None, **kw):
        calls.append({"to": to_phone, "tpl": template_name, "params": template_params})
        return "wamid.TPLSEND"

    monkeypatch.setattr(admin_module, "send_message", fake_send)
    r = await client.post("/admin/api/inbox/send-template", headers=AUTH,
                          json={"phone": P2, "template_name": "kk_order_ready",
                                "params": ["KK-20260804-01"]})
    assert r.status_code == 200, r.text
    assert calls[0]["tpl"] == "kk_order_ready" and calls[0]["to"] == P2
    # customer row auto-created so the thread exists
    async with async_session_factory() as s:
        assert (await s.execute(select(Customer.id).where(Customer.phone == P2))).scalar_one()


async def test_bulk_import(client) -> None:
    text = f"Sharma ji, 99999 00613\n{P3}\nkachra-line\n"
    r = await client.post("/admin/api/customers/bulk", headers=AUTH, json={"text": text})
    assert r.status_code == 200
    d = r.json()
    # first line adds P3 with name; second line same number -> skipped
    assert d["added"] == 1 and d["skipped_existing"] == 1 and len(d["invalid"]) == 1
    async with async_session_factory() as s:
        cust = (await s.execute(select(Customer).where(Customer.phone == P3))).scalar_one()
        assert cust.name == "Sharma ji"
