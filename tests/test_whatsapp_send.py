"""Interactive LIST messages — Meta ka payload aur uski hadd.

Buttons sirf 3 ho sakte hain. Jab staff ke paas 5-6 order/kaam khule ho, to
"kis par jawab diya" ka sawal buttons se hal nahi hota — list se hota hai.
Ye tests wahi payload pakadte hain jo Meta ko jata hai; galat shape par
Meta 400 deta hai aur staff tak kuch nahi pahunchta.
"""

from datetime import datetime, timezone

import pytest

import app.services.whatsapp as whatsapp_module
from app.database import async_session_factory
from app.models import Customer
from app.services.whatsapp import Button, ListRow, send_message
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
                phone=TEST_CUSTOMER_PHONE, name="List Test",
                last_message_at=datetime.now(timezone.utc),
            )
        )
        await s.commit()
    yield


async def _capture(monkeypatch) -> list[dict]:
    seen: list[dict] = []

    async def _post_ok(payload, to_phone, creds=None):
        seen.append(payload)
        return {"messages": [{"id": "wamid.TESTLIST"}]}

    monkeypatch.setattr(whatsapp_module, "_post_with_retry", _post_ok)
    return seen


async def test_list_payload_matches_metas_shape(open_window, monkeypatch) -> None:
    seen = await _capture(monkeypatch)
    async with async_session_factory() as db:
        await send_message(
            db, to_phone=TEST_CUSTOMER_PHONE, text="Aaj ka kaam",
            list_rows=ROWS, list_button="Kaam chuniye", list_title="Aaj ka kaam",
        )
    inter = seen[0]["interactive"]
    assert seen[0]["type"] == "interactive" and inter["type"] == "list"
    assert inter["action"]["button"] == "Kaam chuniye"
    rows = inter["action"]["sections"][0]["rows"]
    assert [r["id"] for r in rows] == ["pick:o:KK-20260809-01", "pick:t:T-11"]
    assert rows[0]["description"] == "Pooja — 1 x Lehenga"


async def test_a_row_without_a_description_omits_the_key(open_window, monkeypatch) -> None:
    """Khali description bhejna Meta ko pasand nahi — key hi na jaye."""
    seen = await _capture(monkeypatch)
    async with async_session_factory() as db:
        await send_message(
            db, to_phone=TEST_CUSTOMER_PHONE, text="Chuniye",
            list_rows=[ListRow(id="pick:t:T-1", title="T-1")],
        )
    row = seen[0]["interactive"]["action"]["sections"][0]["rows"][0]
    assert row == {"id": "pick:t:T-1", "title": "T-1"}


async def test_metas_limits_are_refused_before_the_call(open_window, monkeypatch) -> None:
    """Hadd todne par apne hi ghar mein rukna hai — Meta ke 400 se pehle."""
    seen = await _capture(monkeypatch)
    async with async_session_factory() as db:
        with pytest.raises(ValueError, match="max 10 list rows"):
            await send_message(
                db, to_phone=TEST_CUSTOMER_PHONE, text="x",
                list_rows=[ListRow(id=f"r{i}", title=f"row {i}") for i in range(11)],
            )
        with pytest.raises(ValueError, match="row title"):
            await send_message(
                db, to_phone=TEST_CUSTOMER_PHONE, text="x",
                list_rows=[ListRow(id="r", title="y" * 25)],
            )
        with pytest.raises(ValueError, match="row description"):
            await send_message(
                db, to_phone=TEST_CUSTOMER_PHONE, text="x",
                list_rows=[ListRow(id="r", title="ok", description="z" * 73)],
            )
        with pytest.raises(ValueError, match="buttons/list need a text body"):
            await send_message(db, to_phone=TEST_CUSTOMER_PHONE, list_rows=ROWS)
        with pytest.raises(ValueError, match="either buttons or a list"):
            await send_message(
                db, to_phone=TEST_CUSTOMER_PHONE, text="x",
                buttons=[Button("a", "A")], list_rows=ROWS,
            )
    assert seen == [], "in mein se koi call Meta tak nahi jani chahiye"


async def test_the_inbox_shows_what_the_list_offered(open_window, monkeypatch) -> None:
    """Owner ke Inbox mein '[list: ...]' dikhe — warna wo dekh hi nahi paata
    ki staff ko kaunse option bheje gaye the."""
    await _capture(monkeypatch)
    from sqlalchemy import select

    from app.models import Conversation

    async with async_session_factory() as db:
        await send_message(
            db, to_phone=TEST_CUSTOMER_PHONE, text="Aaj ka kaam", list_rows=ROWS,
        )
    async with async_session_factory() as s:
        conv = (
            await s.execute(
                select(Conversation).where(Conversation.wa_message_id == "wamid.TESTLIST")
            )
        ).scalar_one()
    assert "[list:" in conv.message_text and "KK-20260809-01" in conv.message_text
