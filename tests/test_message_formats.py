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


# --- har shop apne shabd rakh sake ----------------------------------------


async def test_a_shop_can_rename_the_staff_buttons() -> None:
    """Button ke shabd code mein gade nahi hain — har client apne rakh sake.

    Ek dukaan "✅ Ho gaya" rakhe, doosri "Done", teesri apni bhasha mein.
    Button ki ID nahi badalti, sirf dikhne wala naam — isliye handler
    waise ke waise chalte rehte hain.
    """
    from app.database import async_session_factory
    from app.services import app_settings
    from app.services.work_orders import order_buttons, task_buttons

    async with async_session_factory() as db:
        prior = {
            k: await app_settings.get(db, k)
            for k in ("agent_btn_done", "agent_btn_later", "agent_btn_problem")
        }
        try:
            await app_settings.set_value(db, "agent_btn_done", "Done ✔")
            btns = await order_buttons(db, "KK-20260101-01")
            assert btns[0].title == "Done ✔"
            assert btns[0].id == "ord:KK-20260101-01:done", "id kabhi na badle"
            tb = await task_buttons(db, "T-1")
            assert tb[0].title == "Done ✔" and tb[0].id == "task:T-1:done"
        finally:
            async with async_session_factory() as d2:
                for k, v in prior.items():
                    await app_settings.set_value(d2, k, v)


async def test_a_too_long_label_never_breaks_the_send() -> None:
    """Meta 20 akshar se lamba button title reject karta hai — us par poora
    message girta hai. Shop kuch bhi likhe, hum chhota kar ke bhejte hain."""
    from app.database import async_session_factory
    from app.services import app_settings
    from app.services.work_orders import list_button_label, order_buttons

    async with async_session_factory() as db:
        prior_btn = await app_settings.get(db, "agent_btn_later")
        prior_list = await app_settings.get(db, "agent_list_button")
        try:
            await app_settings.set_value(db, "agent_btn_later", "b" * 60)
            await app_settings.set_value(db, "agent_list_button", "c" * 60)
            btns = await order_buttons(db, "KK-20260101-01")
            assert len(btns[1].title) == 20
            assert len(await list_button_label(db)) == 20
            # khali chhodne par default wapas
            await app_settings.set_value(db, "agent_btn_later", "")
            assert (await order_buttons(db, "KK-20260101-01"))[1].title == "⏳ Time lagega"
        finally:
            async with async_session_factory() as d2:
                await app_settings.set_value(d2, "agent_btn_later", prior_btn)
                await app_settings.set_value(d2, "agent_list_button", prior_list)


async def test_a_shop_can_add_its_own_trouble_words() -> None:
    """Har dukaan ki bol-chaal alag — "reject", "faulty", apni bhasha ka
    shabd bhi dikkat maana jaye, code chhue bina."""
    from app.database import async_session_factory
    from app.services.bill_agent import _shop_trouble_word
    from app.services import app_settings

    async with async_session_factory() as db:
        prior = await app_settings.get(db, "agent_trouble_words")
        try:
            assert not await _shop_trouble_word(db, "ye piece reject hai")
            await app_settings.set_value(db, "agent_trouble_words", ["reject", "faulty"])
            assert await _shop_trouble_word(db, "ye piece REJECT hai")
            assert not await _shop_trouble_word(db, "sab theek hai")
        finally:
            async with async_session_factory() as d2:
                await app_settings.set_value(d2, "agent_trouble_words", prior)
