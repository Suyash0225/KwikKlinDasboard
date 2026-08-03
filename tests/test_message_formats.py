"""Owner-edited message formats: save, hot-apply, validate, reset, persist."""

import pytest
from sqlalchemy import text as sqltext

from app.config import settings
from app.database import async_session_factory
from app.services.messages import get_message, load_overrides, set_override

AUTH = {"X-API-Key": settings.ADMIN_API_KEY}


@pytest.fixture(autouse=True)
async def _cleanup():
    yield
    set_override("order_ready", None)
    load_overrides({})
    async with async_session_factory() as s:
        await s.execute(sqltext("DELETE FROM settings_kv WHERE key = 'message_overrides'"))
        await s.commit()


async def test_edit_applies_immediately_and_persists(client) -> None:
    r = await client.put("/admin/api/message-formats", headers=AUTH, json={
        "key": "order_ready",
        "text": "Ho gaya taiyar {order_number}! Aa jao le jao 🎉 — {shop}",
    })
    assert r.status_code == 200
    # hot-applied: get_message now uses the owner's format
    msg = get_message("order_ready", order_number="KK-1")
    assert msg.startswith("Ho gaya taiyar KK-1!")
    # persisted: a fresh load from the kv store restores it
    from app.services import app_settings

    load_overrides({})
    assert "Khushkhabri" in get_message("order_ready", order_number="KK-1")
    async with async_session_factory() as db:
        load_overrides(await app_settings.get(db, "message_overrides"))
    assert get_message("order_ready", order_number="KK-1").startswith("Ho gaya taiyar")

    # listing shows it as overridden
    rows = (await client.get("/admin/api/message-formats", headers=AUTH)).json()
    row = next(x for x in rows if x["key"] == "order_ready")
    assert row["overridden"] is True and row["current"].startswith("Ho gaya")


async def test_bad_placeholder_rejected(client) -> None:
    r = await client.put("/admin/api/message-formats", headers=AUTH, json={
        "key": "order_ready", "text": "Ready {customer_bank_account}!",
    })
    assert r.status_code == 400
    assert "customer_bank_account" in r.json()["detail"]


async def test_reset_restores_default(client) -> None:
    await client.put("/admin/api/message-formats", headers=AUTH, json={
        "key": "order_ready", "text": "Custom {order_number}",
    })
    r = await client.put("/admin/api/message-formats", headers=AUTH, json={
        "key": "order_ready", "text": "",
    })
    assert r.status_code == 200 and r.json()["overridden"] is False
    assert "Khushkhabri" in get_message("order_ready", order_number="KK-1")


async def test_non_editable_key_rejected(client) -> None:
    r = await client.put("/admin/api/message-formats", headers=AUTH, json={
        "key": "escalation_alert", "text": "hack"
    })
    assert r.status_code == 400
