"""⚠️ Damage photo: staff pickup par le, grahak ko WhatsApp par jaye aur
bill page par dikhe — "aapne kharab kiya" ka jhagda khatam."""

from pathlib import Path

from sqlalchemy import select

from app.database import async_session_factory
from app.models import Order, Staff
from app.services import background, bill_link, tenant_context, whatsapp
from app.services.order_service import create_order
from tests.test_staff_panel import A_DEL_PHONE, PW, _login, two_shops  # noqa: F401

CUST = "+919999900208"
MEDIA = Path(__file__).resolve().parent.parent / "app" / "media"
# 1x1 JPEG header is enough: the endpoint checks content-type and emptiness
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9"


async def _order_for(tenant_id, staff_id, phone: str):
    from app.services import tasks as task_service

    async with tenant_context.as_tenant(tenant_id):
        async with async_session_factory() as db:
            order = await create_order(
                db, customer_phone=phone, customer_name="Daag Grahak",
                items=[{"type": "Saree", "service": "Dry Clean", "qty": 1, "rate": 250, "amount": 250}],
                total_amount=250, created_by="test",
            )
            st = await db.get(Staff, staff_id)
            await task_service.create_task(db, title="Pickup", staff=st, order=order, notify=False, kind="pickup")
            return order.order_number, order.id


async def _photos(tenant_id, number):
    async with tenant_context.as_tenant(tenant_id):
        async with async_session_factory() as db:
            o = (await db.execute(select(Order).where(Order.order_number == number))).scalar_one()
            return list(o.customer_photos or []), o.notes or ""


async def test_damage_photo_reaches_customer_on_whatsapp_and_bill_page(client, two_shops, sent, monkeypatch) -> None:
    number, oid = await _order_for(two_shops["a"], two_shops["a_del"], CUST)
    images = []

    async def fake_send_image(db, *, to_phone, file_path, mime_type, caption=None, **kw):
        images.append({"to": to_phone, "path": file_path, "caption": caption or "", **kw})
        return "wamid.FAKEIMG"

    monkeypatch.setattr(whatsapp, "send_image", fake_send_image)
    await _login(client, A_DEL_PHONE)
    files = []
    try:
        r = await client.post(
            f"/staff/api/orders/{number}/photo",
            files={"photo": ("saree.jpg", JPEG, "image/jpeg")},
            data={"kind": "damage", "note": "collar par purana daag"},
        )
        assert r.status_code == 201, r.text
        files.append(r.json()["file"])
        assert r.json()["sent_to_customer"] is True
        await background.drain()

        # grahak ko photo + wajah + bill link
        assert len(images) == 1 and images[0]["to"] == CUST
        assert number in images[0]["caption"] and "collar par purana daag" in images[0]["caption"]
        assert images[0]["intent"] == "transactional" and Path(images[0]["path"]).is_file()

        photos, notes = await _photos(two_shops["a"], number)
        assert [p["file"] for p in photos] == files and photos[0]["note"] == "collar par purana daag"
        assert "⚠️ damage photo" in notes, "owner ke notes mein pehle jaisa"

        # bill page: 'Condition at pickup' + photo sirf signed link se
        tok = bill_link.make(two_shops["a"], oid)
        html = (await client.get(f"/b/{tok}")).text
        assert "Condition at pickup" in html and f"/b/{tok}/photo/{files[0]}" in html
        img = await client.get(f"/b/{tok}/photo/{files[0]}")
        assert img.status_code == 200 and img.headers["content-type"].startswith("image/jpeg")
        assert img.content == JPEG
        assert (await client.get(f"/b/{tok}/photo/job-nahi-hai.jpg")).status_code == 404
        other = bill_link.make(two_shops["b"], oid)
        assert (await client.get(f"/b/{other}/photo/{files[0]}")).status_code == 404, "doosri dukaan ka token"

        # aam photo (general): owner ko, grahak ko nahi
        r = await client.post(
            f"/staff/api/orders/{number}/photo",
            files={"photo": ("bag.jpg", JPEG, "image/jpeg")}, data={"kind": "general", "note": "12 pcs"},
        )
        assert r.status_code == 201 and r.json()["sent_to_customer"] is False
        files.append(r.json()["file"])
        await background.drain()
        assert len(images) == 1
        photos, _ = await _photos(two_shops["a"], number)
        assert len(photos) == 1
        assert "12 pcs" not in (await client.get(f"/b/{tok}")).text
    finally:
        for f in files:
            (MEDIA / f).unlink(missing_ok=True)
