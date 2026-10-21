"""Har service ke apne stages — sirf-press wala kapda wash/dry se nahi guzarta."""

import pytest
from sqlalchemy import text as sqltext

from app.database import async_session_factory
from app.models import OrderStatus
from app.services import service_stages as ss
from tests.test_staff_panel import (  # noqa: F401 (two_shops fixture yahin se aata hai)
    A_PHONE, CUST_A, _login, _order_for, two_shops,
)

W, D, I, R = OrderStatus.IN_WASH, OrderStatus.IN_DRY, OrderStatus.IN_IRON, OrderStatus.READY


def test_default_stages_from_service_name() -> None:
    assert ss.default_stages("Steam Iron") == [I]
    assert ss.default_stages("Saree Polish") == [I]
    assert ss.default_stages("Press") == [I]
    assert ss.default_stages("Wash & Fold (kg)") == [W, D]
    assert ss.default_stages("Wash & Iron") == [W, D, I]
    assert ss.default_stages("Dry Clean") == [W, D, I]
    assert ss.default_stages("") == [W, D, I]


def test_order_stages_are_the_union_in_order() -> None:
    cfg = {"Polish": ["IN_IRON"], "Fold": ["IN_WASH", "IN_DRY"]}
    assert ss.stages_for_services(["Polish"], cfg) == [I]
    assert ss.stages_for_services(["Polish", "Fold"], cfg) == [W, D, I]
    # Owner ka config naam ke andaaze se upar
    assert ss.stages_for_services(["Steam Iron"], {"Steam Iron": ["IN_WASH", "IN_IRON"]}) == [W, I]
    # Koi service hi nahi (purana order) — pehle jaisa poora kram
    assert ss.stages_for_services([], {}) == [W, D, I]


def test_next_after_skips_missing_stages() -> None:
    assert ss.next_after(OrderStatus.RECEIVED, [I]) is I
    assert ss.next_after(I, [I]) is R
    assert ss.next_after(OrderStatus.PICKED_UP, [W, D]) is W
    assert ss.next_after(W, [W, I]) is I
    assert ss.next_after(D, [W, D]) is R
    assert ss.next_after(OrderStatus.RECEIVED, []) is R
    assert ss.next_after(OrderStatus.READY, [W, D, I]) is None


def test_whatsapp_menu_step() -> None:
    assert ss.menu_step(OrderStatus.RECEIVED, [I])[:2] == ("iron", "👔 Start ironing")
    assert ss.menu_step(W, [W, I])[:2] == ("iron", "👔 Washing done")
    assert ss.menu_step(I, [I])[0] == "ready"
    # Deploy se pehle bana "wash" kaam sirf-press order par bhi chale
    assert ss.task_matches("wash", OrderStatus.RECEIVED, [I])
    assert not ss.task_matches("wash", I, [I])


def test_clean_config_drops_unknown_stages() -> None:
    assert ss.clean_config({"Polish": ["IN_IRON", "BOGUS", "IN_IRON"]}) == {"Polish": ["IN_IRON"]}
    with pytest.raises(ValueError):
        ss.clean_config(["IN_IRON"])


async def test_staff_panel_iron_only_order_skips_wash(client, two_shops, sent) -> None:
    num = await _order_for(two_shops["a"], two_shops["a_wash"], CUST_A)
    async with async_session_factory() as db:
        await db.execute(
            sqltext(
                "UPDATE orders SET items = CAST(:i AS jsonb) WHERE order_number = :n"
            ),
            {"i": '[{"type": "Saree", "qty": 1, "service": "Steam Iron"}]', "n": num},
        )
        await db.commit()

    await _login(client, A_PHONE)
    stop = next(s for s in (await client.get("/staff/api/route")).json()["stops"] if s["number"] == num)
    assert stop["next"] == "IN_IRON"

    # Purani screen ka galat stage nahi lagta
    assert (await client.post(f"/staff/api/orders/{num}/advance", json={"to": "IN_WASH"})).status_code == 409
    r = await client.post(f"/staff/api/orders/{num}/advance", json={"to": "IN_IRON"})
    assert r.status_code == 200 and r.json()["status"] == "IN_IRON", r.text
    assert (await client.get(f"/staff/api/orders/{num}")).json()["next"] == "READY"
    r = await client.post(f"/staff/api/orders/{num}/advance", json={"to": "READY"})
    assert r.status_code == 200 and r.json()["status"] == "READY", r.text
