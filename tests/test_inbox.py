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
    mine = [t for t in r.json()["threads"] if t["phone"] == PHONE]
    assert mine and mine[0]["name"] == "Inbox Grahak"
    assert mine[0]["last_text"].startswith("bhaiya kurta")
    assert mine[0]["window"]["open"] is True


async def test_reply_quotes_the_message_on_whatsapp_and_in_the_thread(client) -> None:
    """Answering one message must reach WhatsApp as a real quoted reply."""
    import app.services.whatsapp as wa

    await _seed_customer(window_open=True)
    seen: dict = {}
    orig = wa._post_with_retry

    async def spy(payload, to_phone):
        seen.update(payload)
        return await orig(payload, to_phone)

    wa._post_with_retry = spy
    try:
        r = await client.post(
            "/admin/api/inbox/send", headers=AUTH,
            json={"phone": PHONE, "text": "haan bhaiya, sham tak", "reply_to": "wamid.TESTINBOX-1"},
        )
        assert r.status_code == 200
    finally:
        wa._post_with_retry = orig

    assert seen.get("context") == {"message_id": "wamid.TESTINBOX-1"}, seen
    d = (await client.get(f"/admin/api/inbox/thread?phone={PHONE}", headers=AUTH)).json()
    last = d["messages"][-1]
    assert last["reply_to"] == "wamid.TESTINBOX-1"
    assert last["status"] == "sent" and last["wamid"]


async def test_ping_nudges_the_person(client) -> None:
    await _seed_customer(window_open=True)
    r = await client.post("/admin/api/inbox/ping", headers=AUTH, json={"phone": PHONE})
    assert r.status_code == 200
    assert "🔔" in r.json()["text"]
    # it really went out and is in their thread, not just an API 200
    d = (await client.get(f"/admin/api/inbox/thread?phone={PHONE}", headers=AUTH)).json()
    assert "🔔" in d["messages"][-1]["text"]
    assert d["messages"][-1]["sent_by"] == "manager"


async def test_template_send_keeps_its_words_in_the_thread(client, monkeypatch) -> None:
    """'[template:kk_staff_alert]' alone told the owner nothing about what
    was actually sent — the params ARE the message."""
    from app.services import whatsapp as wa

    await _seed_customer(window_open=True)
    async with async_session_factory() as s:
        await wa.send_message(
            s, to_phone=PHONE,
            template_name="kk_staff_alert",
            template_params=["Ajit ne bola: sham tak deliver kar dunga"],
        )
    r = (await client.get(f"/admin/api/inbox/thread?phone={PHONE}", headers=AUTH)).json()
    last = r["messages"][-1]["text"]
    assert last.startswith("[template:kk_staff_alert]"), last
    assert "sham tak deliver" in last, "asli message thread mein dikhna chahiye"


async def test_one_person_is_one_thread(client) -> None:
    """A number with BOTH a staff row and a customer row (the owner, or a
    worker who once wrote in as a customer) showed up twice, each thread
    holding half his history."""
    from app.models import Staff, StaffRole

    await _seed_customer(window_open=True)
    async with async_session_factory() as s:
        st = Staff(phone=PHONE, name="Inbox Bhaiya", role=StaffRole.DELIVERY)
        s.add(st)
        await s.flush()
        s.add(
            Conversation(
                staff_id=st.id,
                direction=Direction.OUTBOUND,
                message_text="kal ka pickup dekh lena",
                wa_message_id="wamid.TESTINBOX-2",
            )
        )
        await s.commit()
    try:
        rows = (await client.get("/admin/api/inbox/threads", headers=AUTH)).json()["threads"]
        mine = [t for t in rows if t["phone"] == PHONE]
        assert len(mine) == 1, f"ek hi thread honi chahiye, mili {len(mine)}"
        assert mine[0]["kind"] == "staff" and mine[0]["name"] == "Inbox Bhaiya"

        d = (await client.get(f"/admin/api/inbox/thread?phone={PHONE}", headers=AUTH)).json()
        texts = [m["text"] for m in d["messages"]]
        assert "bhaiya kurta ready?" in texts and "kal ka pickup dekh lena" in texts
        assert d["window"]["open"] is True, "customer side ka window bhi ginna chahiye"
    finally:
        async with async_session_factory() as s:
            await s.execute(
                sqltext(
                    "DELETE FROM conversations WHERE staff_id IN "
                    f"(SELECT id FROM staff WHERE phone = '{PHONE}')"
                )
            )
            await s.execute(sqltext(f"DELETE FROM staff WHERE phone = '{PHONE}'"))
            await s.commit()


# --- 500+ contacts: paging, search, and bringing them in from a file ---

IMPORT_PREFIX = "+91919888"         # throwaway range for the import tests


@pytest.fixture
async def _purge_imported():
    yield
    async with async_session_factory() as s:
        await s.execute(
            sqltext(f"DELETE FROM customers WHERE phone LIKE '{IMPORT_PREFIX}%'")
        )
        await s.commit()


async def test_import_csv_with_headers(client, _purge_imported) -> None:
    csv_bytes = (
        "Name,Mobile,Address\n"
        "Sharma Ji,9198880001,Sigra\n"
        "Seema Mam,+91 9198880002,\n"
        ",9198880003,Lanka\n"
    ).encode()
    r = await client.post(
        "/admin/api/customers/import-file", headers=AUTH,
        files={"file": ("contacts.csv", csv_bytes, "text/csv")},
    )
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["added"] == 3 and d["invalid_total"] == 0

    async with async_session_factory() as s:
        row = (
            await s.execute(select(Customer).where(Customer.phone == "+919198880001"))
        ).scalar_one()
    assert row.name == "Sharma Ji" and row.address == "Sigra"


async def test_import_without_headers_and_bad_rows_are_reported(client, _purge_imported) -> None:
    """No heading row, and a junk line that must be shown back, not dropped."""
    csv_bytes = "Ravi Kumar,9198880010\nnot-a-number,hello\n".encode()
    r = await client.post(
        "/admin/api/customers/import-file", headers=AUTH,
        files={"file": ("list.csv", csv_bytes, "text/csv")},
    )
    d = r.json()
    assert d["added"] == 1
    assert d["invalid_total"] == 1 and "line 2" in d["invalid"][0]

    async with async_session_factory() as s:
        row = (
            await s.execute(select(Customer).where(Customer.phone == "+919198880010"))
        ).scalar_one()
    assert row.name == "Ravi Kumar"


async def test_import_fills_blanks_but_never_overwrites(client, _purge_imported) -> None:
    async with async_session_factory() as s:
        s.add(Customer(phone="+919198880020", name="Asli Naam"))
        await s.commit()
    csv_bytes = "name,phone,address\nGalat Naam,9198880020,Bhelupur\n".encode()
    r = await client.post(
        "/admin/api/customers/import-file", headers=AUTH,
        files={"file": ("c.csv", csv_bytes, "text/csv")},
    )
    assert r.json()["updated"] == 1 and r.json()["added"] == 0
    async with async_session_factory() as s:
        row = (
            await s.execute(select(Customer).where(Customer.phone == "+919198880020"))
        ).scalar_one()
    assert row.name == "Asli Naam", "import ko purana naam nahi badalna chahiye"
    assert row.address == "Bhelupur", "khali jagah bhar dena chahiye"


async def test_import_xlsx(client, _purge_imported) -> None:
    import io as _io

    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Name", "Phone"])
    ws.append(["Excel Wala", 9198880030])
    buf = _io.BytesIO()
    wb.save(buf)

    r = await client.post(
        "/admin/api/customers/import-file", headers=AUTH,
        files={"file": ("book.xlsx", buf.getvalue(),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert r.status_code == 200, r.text
    assert r.json()["added"] == 1
    async with async_session_factory() as s:
        row = (
            await s.execute(select(Customer).where(Customer.phone == "+919198880030"))
        ).scalar_one()
    assert row.name == "Excel Wala"


async def test_threads_are_paged_and_searchable(client, _purge_imported) -> None:
    """500+ contacts: the list arrives in pages, and search reaches people
    who have never messaged (a fresh import)."""
    await _seed_customer(window_open=True)
    async with async_session_factory() as s:
        for i in range(5):
            s.add(Customer(phone=f"+9191988810{i:02d}", name=f"Import Wala {i}"))
        await s.commit()

    page = (await client.get("/admin/api/inbox/threads?limit=2&offset=0", headers=AUTH)).json()
    assert len(page["threads"]) <= 2
    assert page["total"] >= 1
    if page["has_more"]:
        nxt = (await client.get("/admin/api/inbox/threads?limit=2&offset=2", headers=AUTH)).json()
        first = {t["phone"] for t in page["threads"]}
        assert not (first & {t["phone"] for t in nxt["threads"]}), "pages must not repeat"

    # never-messaged contacts are findable by name...
    hit = (await client.get("/admin/api/inbox/threads?q=Import Wala", headers=AUTH)).json()
    names = [t["name"] for t in hit["threads"]]
    assert any(n.startswith("Import Wala") for n in names), names
    assert all(t.get("no_messages") for t in hit["threads"] if t["name"].startswith("Import Wala"))
    # ...and by number
    byno = (await client.get("/admin/api/inbox/threads?q=9198881003", headers=AUTH)).json()
    assert [t["phone"] for t in byno["threads"]] == ["+919198881003"]


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


async def test_media_opens_for_a_logged_in_owner(client) -> None:
    """Inbox ki photos login se khulni chahiye.

    Jab dashboard asli login par gaya, browser ke paas admin key rehna band
    ho gayi — har <img> 401 khaata tha aur chat mein toota hua dabba dikhta
    tha. Session cookie hi kaafi honi chahiye.
    """
    from pathlib import Path

    from sqlalchemy import select

    from app.models import ROLE_OWNER, Tenant, User
    from app.services import app_settings, auth

    media_dir = Path("app/media")
    media_dir.mkdir(exist_ok=True)
    test_file = media_dir / "test-session-media.jpg"
    test_file.write_bytes(b"fake-jpg-bytes")

    async with async_session_factory() as s:
        home = (
            await s.execute(select(Tenant).where(Tenant.slug == "kwik-klin"))
        ).scalar_one_or_none()
        if home is None:
            home = Tenant(
                slug="kwik-klin", shop_name="Kwik Klin", owner_name="Suyash",
                owner_phone="+918933871103", plan="growth", status="active",
            )
            s.add(home)
            await s.commit()
        await app_settings.set_value(s, "home_tenant_slug", "kwik-klin")
        user = (
            await s.execute(
                select(User).where(User.tenant_id == home.id, User.role == ROLE_OWNER)
            )
        ).scalars().first()
        if user is None:
            user = User(
                tenant_id=home.id, name="Suyash", email="media-test@example.com",
                password_hash=auth.hash_password("mediapass123"), role=ROLE_OWNER,
            )
            s.add(user)
            await s.commit()
        token = await auth.start_session(s, user)

    try:
        # bina kuch diye -> 401
        assert (await client.get("/admin/media/test-session-media.jpg")).status_code == 401
        # sirf login se -> khul jaaye
        client.cookies.set(auth.SESSION_COOKIE, token)
        r = await client.get("/admin/media/test-session-media.jpg")
        assert r.status_code == 200, "logged-in owner ko photo dikhni chahiye"
        assert r.content == b"fake-jpg-bytes"
    finally:
        client.cookies.clear()
        test_file.unlink(missing_ok=True)


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
