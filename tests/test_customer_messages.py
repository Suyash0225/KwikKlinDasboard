"""Bill par haath se bheje messages + choti Google review link (/r/<slug>)."""

from decimal import Decimal

from sqlalchemy import text as sqltext

from app.config import settings
from app.database import async_session_factory
from app.services import app_settings, order_service, tenant_context
from tests.conftest import purge_phones

AUTH = {"X-API-Key": settings.ADMIN_API_KEY}
PHONE = "+919999900081"
LINK = "https://search.google.com/local/writereview?placeid=ChIJ1WWOI4ozjjkRE_84Vr0lhPs"


async def _set(key, value) -> None:
    async with async_session_factory() as db:
        await app_settings.set_value(db, key, value)


async def _bill(total="500") -> str:
    async with async_session_factory() as db:
        o = await order_service.create_order(
            db, customer_phone=PHONE, customer_name="Pooja", created_by="test",
            items=[{"type": "Saree", "qty": 1}], total_amount=Decimal(total),
        )
        return o.order_number


async def _home_slug() -> str:
    tid = await tenant_context.get_home_tenant_id()
    async with async_session_factory() as db:
        return (await db.execute(sqltext("SELECT slug FROM tenants WHERE id = :t"), {"t": str(tid)})).scalar_one()


async def test_thank_you_messages_are_built_on_the_server(client, sent) -> None:
    old_link = old_base = None
    async with async_session_factory() as db:
        old_link = await app_settings.get(db, "google_review_link")
        old_base = await app_settings.get(db, "public_base_url")
    try:
        number = await _bill("500")
        # payment abhi nahi — thank you ka matlab nahi
        r = await client.get(f"/orders/{number}/message?kind=payment_thanks", headers=AUTH)
        assert r.status_code == 400
        await client.post(f"/orders/{number}/payment", json={"amount": "300", "method": "CASH"}, headers=AUTH)
        r = await client.get(f"/orders/{number}/message?kind=payment_thanks", headers=AUTH)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["phone"] == PHONE
        assert "₹300" in body["text"] and number in body["text"] and "Balance due: ₹200" in body["text"]

        r = await client.get(f"/orders/{number}/message?kind=service_thanks", headers=AUTH)
        assert r.status_code == 200 and "Pooja" in r.json()["text"]

        # review: link na ho to saaf mana
        await _set("google_review_link", "")
        r = await client.get(f"/orders/{number}/message?kind=review_request", headers=AUTH)
        assert r.status_code == 400 and "review link" in r.json()["detail"]

        # link ho + public URL ho -> choti link message mein
        await _set("google_review_link", LINK)
        await _set("public_base_url", "https://shop.example.in/")
        r = await client.get(f"/orders/{number}/message?kind=review_request", headers=AUTH)
        slug = await _home_slug()
        assert f"https://shop.example.in/r/{slug}" in r.json()["text"]
        assert "placeid" not in r.json()["text"]

        # anjaan kind
        assert (await client.get(f"/orders/{number}/message?kind=party", headers=AUTH)).status_code == 422
    finally:
        await _set("google_review_link", old_link or "")
        await _set("public_base_url", old_base or "")
        await purge_phones(PHONE)


async def test_short_review_link_redirects_to_google(client) -> None:
    async with async_session_factory() as db:
        old = await app_settings.get(db, "google_review_link")
    slug = await _home_slug()
    try:
        await _set("google_review_link", LINK)
        r = await client.get(f"/r/{slug}", follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == LINK
        assert (await client.get("/r/no-such-shop", follow_redirects=False)).status_code == 404
        await _set("google_review_link", "")
        assert (await client.get(f"/r/{slug}", follow_redirects=False)).status_code == 404
    finally:
        await _set("google_review_link", old or "")
