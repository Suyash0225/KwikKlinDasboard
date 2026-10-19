"""Interactive WhatsApp list messages through the production WAHA gateway.

The application is WAHA/NOWEB-only. These tests therefore assert the payload
that our WAHA adapter sends, rather than an obsolete Meta Graph payload.
"""

from datetime import datetime, timezone

import pytest

import app.services.whatsapp as whatsapp_module
from app.database import async_session_factory
from sqlalchemy import select
from app.models import Customer, Conversation, Staff, StaffRole
from app.services import waha
from app.services.whatsapp import Button, ListRow, SendError, send_message
from tests.conftest import TEST_CUSTOMER_PHONE


ROWS = [
    ListRow(id="pick:o:KK-20260809-01", title="KK-20260809-01", description="Pooja — 1 x Lehenga"),
    ListRow(id="pick:t:T-11", title="T-11 · press", description="5 kapde press karne hain"),
]


@pytest.fixture
async def open_window():
    async with async_session_factory() as s:
        s.add(
            Customer(
                phone=TEST_CUSTOMER_PHONE,
                name="List Test",
                last_message_at=datetime.now(timezone.utc),
            )
        )
        await s.commit()
    yield


async def _capture(monkeypatch) -> list[dict]:
    seen: list[dict] = []

    async def _post(path, payload):
        seen.append({"path": path, "payload": payload})
        return {"id": "wamid.TESTLIST"}

    monkeypatch.setattr(waha, "_post", _post)
    return seen


async def test_inactive_staff_is_not_an_outbound_recipient() -> None:
    """Disabled staff accounts must not receive retries or automated notifications."""
    from app.services.whatsapp import _find_recipient

    phone = "+919999901987"
    async with async_session_factory() as db:
        staff = Staff(phone=phone, name="Inactive Staff Guard", role=StaffRole.WASHER, is_active=False)
        db.add(staff)
        await db.commit()
        staff_id = staff.id
    try:
        async with async_session_factory() as db:
            customer, resolved_staff = await _find_recipient(db, phone)
        assert customer is None
        assert resolved_staff is None
    finally:
        async with async_session_factory() as db:
            staff = await db.get(Staff, staff_id)
            if staff is not None:
                await db.delete(staff)
                await db.commit()


async def test_automated_customer_send_requires_explicit_intent(open_window) -> None:
    async with async_session_factory() as db:
        with pytest.raises(SendError, match="requires explicit outbound intent"):
            await send_message(db, to_phone=TEST_CUSTOMER_PHONE, text="automated reply")


async def test_unknown_staff_recipient_is_rejected(open_window, monkeypatch) -> None:
    """An unknown phone must not be treated as an authorized staff recipient."""
    async def _must_not_send(*args, **kwargs):
        raise AssertionError("WAHA transport must not be called for an unknown recipient")

    monkeypatch.setattr(waha, "_post", _must_not_send)
    async with async_session_factory() as db:
        with pytest.raises(SendError, match="recipient is not a tenant customer or lead"):
            await send_message(
                db,
                to_phone="+919876543299",
                text="This must never be sent",
                sent_by="manager",
                intent="transactional",
            )


async def test_list_payload_matches_waha_shape(open_window, monkeypatch) -> None:
    seen = await _capture(monkeypatch)
    async with async_session_factory() as db:
        await send_message(
            db,
            to_phone=TEST_CUSTOMER_PHONE,
            sent_by="manager",
            text="Aaj ka kaam",
            list_rows=ROWS,
            list_button="Kaam chuniye",
            list_title="Aaj ka kaam",
        )

    assert seen[0]["path"] == "/api/sendList"
    message = seen[0]["payload"]["message"]
    assert seen[0]["payload"]["session"]
    assert seen[0]["payload"]["chatId"].endswith("@c.us")
    assert message["title"] == "Aaj ka kaam"
    assert message["description"].startswith("Aaj ka kaam")
    assert message["button"] == "Kaam chuniye"
    rows = message["sections"][0]["rows"]
    assert [r["rowId"] for r in rows] == ["pick:o:KK-20260809-01", "pick:t:T-11"]
    assert rows[0]["description"] == "Pooja — 1 x Lehenga"


async def test_button_input_is_translated_to_a_waha_list(open_window, monkeypatch) -> None:
    seen = await _capture(monkeypatch)
    async with async_session_factory() as db:
        await send_message(
            db,
            to_phone=TEST_CUSTOMER_PHONE,
            sent_by="manager",
            text="Confirm karo",
            buttons=[Button("order:123:done", "Haan, ho gayi")],
            list_title="Task status",
        )

    message = seen[0]["payload"]["message"]
    assert message["button"] == "Chuniye"
    assert message["sections"][0]["rows"] == [
        {"rowId": "order:123:done", "title": "Haan, ho gayi", "description": ""}
    ]


async def test_empty_description_is_kept_as_a_safe_waha_value(open_window, monkeypatch) -> None:
    seen = await _capture(monkeypatch)
    async with async_session_factory() as db:
        await send_message(
            db,
            to_phone=TEST_CUSTOMER_PHONE,
            sent_by="manager",
            text="Chuniye",
            list_rows=[ListRow(id="pick:t:T-1", title="T-1")],
        )

    row = seen[0]["payload"]["message"]["sections"][0]["rows"][0]
    assert row == {"rowId": "pick:t:T-1", "title": "T-1", "description": ""}


async def test_list_row_limit_is_refused_before_transport(open_window, monkeypatch) -> None:
    seen = await _capture(monkeypatch)
    async with async_session_factory() as db:
        with pytest.raises(ValueError, match="max 10 list rows"):
            await send_message(
                db,
                to_phone=TEST_CUSTOMER_PHONE,
            sent_by="manager",
                text="x",
                list_rows=[ListRow(id=f"r{i}", title=f"row {i}") for i in range(11)],
            )
        with pytest.raises(ValueError, match="buttons/list need a text body"):
            await send_message(db, to_phone=TEST_CUSTOMER_PHONE,
            sent_by="manager", list_rows=ROWS)
        with pytest.raises(ValueError, match="either buttons or a list"):
            await send_message(
                db,
                to_phone=TEST_CUSTOMER_PHONE,
            sent_by="manager",
                text="x",
                buttons=[Button("a", "A")],
                list_rows=ROWS,
            )
    assert seen == []


async def test_the_inbox_records_what_the_list_offered(open_window, monkeypatch) -> None:
    await _capture(monkeypatch)
    async with async_session_factory() as db:
        await send_message(
            db,
            to_phone=TEST_CUSTOMER_PHONE,
            sent_by="manager",
            text="Aaj ka kaam",
            list_rows=ROWS,
        )

    async with async_session_factory() as s:
        conv = (
            await s.execute(
                select(Conversation).where(
                    Conversation.wa_message_id == "wamid.TESTLIST"
                )
            )
        ).scalar_one()

    assert "[list:" in conv.message_text
    assert "KK-20260809-01" in conv.message_text
