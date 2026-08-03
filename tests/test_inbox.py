"""Inbox API tests: threads list, thread fetch, manual send, window lock."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text as sqltext

from app.config import settings
from app.database import async_session_factory
from app.models import Conversation, Customer, Direction

PHONE = "+919999900088"
AUTH = {"X-API-Key": settings.ADMIN_API_KEY}


@pytest.fixture(autouse=True)
async def _cleanup():
    yield
    async with async_session_factory() as s:
        await s.execute(
            sqltext(
                "DELETE FROM conversations WHERE customer_id IN "
                f"(SELECT id FROM customers WHERE phone = '{PHONE}')"
            )
        )
        await s.execute(sqltext(f"DELETE FROM customers WHERE phone = '{PHONE}'"))
        await s.commit()


async def _seed_customer(window_open: bool) -> None:
    async with async_session_factory() as s:
        last = datetime.now(timezone.utc) - (
            timedelta(hours=1) if window_open else timedelta(hours=30)
        )
        cust = Customer(phone=PHONE, name="Inbox Grahak", last_message_at=last)
        s.add(cust)
        await s.flush()
        s.add(
            Conversation(
                customer_id=cust.id,
                direction=Direction.INBOUND,
                message_text="bhaiya kurta ready?",
                wa_message_id="wamid.TESTINBOX-1",
            )
        )
        await s.commit()


async def test_inbox_requires_auth(client) -> None:
    assert (await client.get("/admin/api/inbox/threads")).status_code == 401
    assert (await client.get("/admin/api/inbox/thread?phone=x")).status_code == 401
    assert (
        await client.post("/admin/api/inbox/send", json={"phone": PHONE, "text": "hi"})
    ).status_code == 401


async def test_threads_list_shows_participant(client) -> None:
    await _seed_customer(window_open=True)
    r = await client.get("/admin/api/inbox/threads", headers=AUTH)
    assert r.status_code == 200
    mine = [t for t in r.json() if t["phone"] == PHONE]
    assert mine and mine[0]["name"] == "Inbox Grahak"
    assert mine[0]["last_text"].startswith("bhaiya kurta")
    assert mine[0]["window"]["open"] is True


async def test_thread_fetch_messages_and_window(client) -> None:
    await _seed_customer(window_open=False)
    r = await client.get(f"/admin/api/inbox/thread?phone={PHONE}", headers=AUTH)
    assert r.status_code == 200
    d = r.json()
    assert d["kind"] == "customer"
    assert d["window"]["open"] is False
    assert [m["text"] for m in d["messages"]] == ["bhaiya kurta ready?"]
    # unknown phone -> 404
    assert (
        await client.get("/admin/api/inbox/thread?phone=%2B919999900999", headers=AUTH)
    ).status_code == 404


async def test_manager_send_records_sent_by(client, sent, monkeypatch) -> None:
    """Manager reply goes out via the single door and is stored as 'manager'."""
    import app.services.whatsapp as whatsapp_module
    import app.routers.admin as admin_module

    # patch the REAL door's HTTP call only, so conversation-recording still runs
    async def fake_post(payload, to_phone):
        return {"messages": [{"id": "wamid.TESTINBOX-OUT1"}]}

    monkeypatch.setattr(whatsapp_module, "_post_with_retry", fake_post)
    # admin.py imported send_message directly; restore the real one (the
    # shared `sent` fixture stubs it in other import sites)
    monkeypatch.setattr(admin_module, "send_message", whatsapp_module.send_message)

    await _seed_customer(window_open=True)
    r = await client.post(
        "/admin/api/inbox/send", json={"phone": PHONE, "text": "haan ji, ready hai"}, headers=AUTH
    )
    assert r.status_code == 200, r.text

    async with async_session_factory() as s:
        conv = (
            await s.execute(
                select(Conversation).where(Conversation.wa_message_id == "wamid.TESTINBOX-OUT1")
            )
        ).scalar_one()
        assert conv.sent_by == "manager"
        assert conv.direction is Direction.OUTBOUND


async def test_media_serve_requires_key(client) -> None:
    from pathlib import Path

    media_dir = Path("app/media")
    media_dir.mkdir(exist_ok=True)
    test_file = media_dir / "test-qa.jpg"
    test_file.write_bytes(b"fake-jpg-bytes")
    try:
        assert (await client.get("/admin/media/test-qa.jpg")).status_code == 401
        r = await client.get(f"/admin/media/test-qa.jpg?key={settings.ADMIN_API_KEY}")
        assert r.status_code == 200
        # traversal must not escape the media dir
        r2 = await client.get(f"/admin/media/..%2F..%2F.env?key={settings.ADMIN_API_KEY}")
        assert r2.status_code == 404
    finally:
        test_file.unlink(missing_ok=True)


async def test_send_media_endpoint_auth_and_validation(client, monkeypatch) -> None:
    import app.routers.admin as admin_module

    async def fake_send_image(db, **kw):
        return "wamid.MEDIA-TEST"

    monkeypatch.setattr(admin_module, "send_image", fake_send_image)
    files = {"file": ("photo.jpg", b"jpg-bytes", "image/jpeg")}

    # no key -> 401
    r = await client.post("/admin/api/inbox/send-media", data={"phone": PHONE}, files=files)
    assert r.status_code == 401
    # non-image -> 400
    r = await client.post(
        "/admin/api/inbox/send-media", data={"phone": PHONE},
        files={"file": ("x.pdf", b"pdf", "application/pdf")}, headers=AUTH,
    )
    assert r.status_code == 400
    # valid -> 200 via mocked sender
    await _seed_customer(window_open=True)
    r = await client.post(
        "/admin/api/inbox/send-media", data={"phone": PHONE, "caption": "bill ki photo"},
        files=files, headers=AUTH,
    )
    assert r.status_code == 200, r.text
    assert r.json()["wa_message_id"] == "wamid.MEDIA-TEST"


async def test_manager_send_blocked_outside_window(client, monkeypatch) -> None:
    import app.services.whatsapp as whatsapp_module
    import app.routers.admin as admin_module

    monkeypatch.setattr(admin_module, "send_message", whatsapp_module.send_message)
    await _seed_customer(window_open=False)
    r = await client.post(
        "/admin/api/inbox/send", json={"phone": PHONE, "text": "suno"}, headers=AUTH
    )
    assert r.status_code == 409
    assert "window is closed" in r.json()["detail"]
