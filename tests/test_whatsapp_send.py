from app.database import async_session_factory
from app.models import Customer
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
                phone=TEST_CUSTOMER_PHONE, name="List Test",
                last_message_at=datetime.now(timezone.utc),
            )
        )
        await s.commit()
    yield



async def test_unknown_recipient_is_rejected(open_window, monkeypatch) -> None:
    async def _must_not_send(*args, **kwargs):
        raise AssertionError("WAHA must not be called for an unauthorized recipient")

    monkeypatch.setattr(waha, "send_text", _must_not_send)
    async with async_session_factory() as db:
        with pytest.raises(SendError, match="not authorized"):
            await send_message(
                db,
                to_phone="+919876543210",
                text="This must never be sent",
                sent_by="bot",
            )


async def _capture(monkeypatch) -> list[dict]:
    seen: list[dict] = []

    async def _list_ok(phone, text, rows, *, button="Choose", title="Kwik Klin"):
        seen.append({
            "phone": phone,
            "text": text,
            "rows": rows,
            "button": button,
            "title": title,
        })
        return "wamid.TESTLIST"

    monkeypatch.setattr(waha, "send_list", _list_ok)
    return seen


async def test_list_payload_matches_metas_shape(open_window, monkeypatch) -> None: