"""Press wala (IRONER) — dhulai ke baad kapde uske paas.

Dhulai ki kataar saanjhi hai: order chahe kisi bhi washerman ke naam ho,
doosra washerman ya press wala bhi uska status aage badha sakta hai.
"""

import pytest

from app.models import StaffRole
from tests.test_staff_panel import (  # noqa: F401 (two_shops fixture yahin se aata hai)
    A_PHONE, CUST_A, _login, _order_for, _purge_staff, _staff, _task_for, two_shops,
)

A_WASH2_PHONE = "+919999900211"  # dukaan A ka doosra washerman
A_IRON_PHONE = "+919999900212"   # dukaan A ka press wala


@pytest.fixture
async def floor(two_shops):
    await _purge_staff(A_WASH2_PHONE, A_IRON_PHONE)
    ids = {
        **two_shops,
        "a_wash2": await _staff(two_shops["a"], A_WASH2_PHONE, "Awash2", StaffRole.WASHER),
        "a_iron": await _staff(two_shops["a"], A_IRON_PHONE, "Airon", StaffRole.IRONER),
    }
    yield ids
    await _purge_staff(A_WASH2_PHONE, A_IRON_PHONE)


async def test_other_washer_and_ironer_move_an_order_assigned_to_someone_else(client, floor, sent) -> None:
    # Order pehle washerman (Awash) ke naam hai
    num = await _order_for(floor["a"], floor["a_wash"], CUST_A, total=300)

    # Doosra washerman dhulai shuru kar sakta hai — pehle "not yours" aata tha
    await _login(client, A_WASH2_PHONE)
    assert num in [s["number"] for s in (await client.get("/staff/api/route")).json()["stops"]]
    r = await client.post(f"/staff/api/orders/{num}/washing")
    assert r.status_code == 200 and r.json()["status"] == "IN_WASH", r.text

    # Press wale ko order dikhta hai, wahi press par bhejta hai aur Ready karta hai
    await _login(client, A_IRON_PHONE)
    me = (await client.get("/staff/api/me")).json()
    assert me["role"] == "IRONER" and me["can_money"] is False and me["can_contact"] is False
    assert num in [s["number"] for s in (await client.get("/staff/api/route")).json()["stops"]]
    assert (await client.get(f"/staff/api/orders/{num}")).status_code == 200
    r = await client.post(f"/staff/api/orders/{num}/ironing")
    assert r.status_code == 200 and r.json()["status"] == "IN_IRON", r.text
    assert (await client.post(f"/staff/api/orders/{num}/ironing")).status_code == 409  # dobara nahi
    r = await client.post(f"/staff/api/orders/{num}/ready")
    assert r.status_code == 200 and r.json()["status"] == "READY", r.text

    # Ready ke baad wo order delivery ka hai — press wale ke liye band
    assert (await client.get(f"/staff/api/orders/{num}")).status_code == 403


async def test_washer_can_send_to_iron_too(client, floor, sent) -> None:
    num = await _order_for(floor["a"], floor["a_wash"], CUST_A)
    await _login(client, A_PHONE)
    assert (await client.post(f"/staff/api/orders/{num}/washing")).status_code == 200
    r = await client.post(f"/staff/api/orders/{num}/ironing")
    assert r.status_code == 200 and r.json()["status"] == "IN_IRON", r.text


async def test_owner_task_for_ironer_reaches_their_panel(client, floor, sent) -> None:
    """Owner ka diya alag kaam press wale ke panel par aata hai, kind ke saath
    — panel isi se use order ki row mein alag dikhata hai."""
    code = await _task_for(floor["a"], floor["a_iron"], CUST_A, "Saree par extra starch")
    await _login(client, A_IRON_PHONE)
    tasks = (await client.get("/staff/api/tasks?tab=mine")).json()["tasks"]
    mine = next(t for t in tasks if t["code"] == code)
    assert mine["title"] == "Saree par extra starch" and mine["kind"] == "general"
