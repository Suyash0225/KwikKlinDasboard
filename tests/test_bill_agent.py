"""Bill-by-text agent tests — LLM mocked; DB, pricing and confirm loop real."""

from datetime import date, timedelta

import pytest
from sqlalchemy import select, text as sqltext

import app.services.bill_agent as bill_module
from app.database import async_session_factory
from app.models import Customer, Order, OrderStatus, Rate
from app.services.bill_agent import _PENDING, handle_staff_message
from app.services.llm_client import LLMUnavailable
from app.services.order_service import create_order

SENDER = "+911111100001"          # pretend staff/manager phone
CUST_PHONE = "+919999900124"      # bill target customer
SERVICE = "TestServiceX"          # our own rate rows -> deterministic pricing


def _extract_result(**overrides) -> dict:
    base = {
        "action": "other",
        "customer_name": "",
        "customer_phone": "",
        "items": [],
        "advance": 0,
        "expected_delivery": "",
        "order_number": "",
        "new_date": "",
        "reason": "",
        "new_status": "NONE",
        "relay_to": "",
        "relay_message": "",
        "priority": "NONE",
        "staff_name": "",
        "note": "",
        "amount": 0,
        "method": "NONE",
        "done_refs": [],
        "pending_refs": [],
        "problem": "",
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
async def _setup_and_cleanup():
    _PENDING.clear()
    from tests.conftest import purge_phones

    await purge_phones(CUST_PHONE)  # crashed earlier runs must not poison this one
    async with async_session_factory() as s:
        # idempotent: a crashed earlier run may have left the row behind
        await s.execute(sqltext(f"DELETE FROM rate_card WHERE service = '{SERVICE}'"))
        s.add(Rate(service=SERVICE, garment="Kurta", unit="pc", rate=40))
        await s.commit()
    yield
    _PENDING.clear()
    from tests.conftest import purge_phones

    await purge_phones(CUST_PHONE)
    async with async_session_factory() as s:
        await s.execute(sqltext(f"DELETE FROM rate_card WHERE service = '{SERVICE}'"))
        await s.commit()


def _patch_extract(monkeypatch, result: dict):
    async def fake_ask_json(**kw):
        return result

    monkeypatch.setattr(bill_module.llm_client, "ask_json", fake_ask_json)


async def _order_for_customer() -> Order | None:
    async with async_session_factory() as s:
        return (
            await s.execute(
                select(Order)
                .join(Customer, Customer.id == Order.customer_id)
                .where(Customer.phone == CUST_PHONE)
            )
        ).scalar_one_or_none()


async def test_bill_draft_then_confirm_creates_order(monkeypatch, sent) -> None:
    _patch_extract(
        monkeypatch,
        _extract_result(
            action="new_bill",
            customer_name="Sharma ji",
            customer_phone="9999900124",
            items=[{"service": SERVICE, "garment": "Kurta", "qty": 2}],
            advance=20,
        ),
    )
    async with async_session_factory() as db:
        draft_reply = await handle_staff_message(
            db, sender_phone=SENDER, sender_label="manager", text="Sharma ji 2 kurta"
        )
    assert draft_reply is not None
    assert "₹80" in draft_reply and "Advance: ₹20" in draft_reply
    assert "Sharma ji" in draft_reply and SENDER in _PENDING
    assert await _order_for_customer() is None  # nothing written yet!

    async with async_session_factory() as db:
        done = await handle_staff_message(
            db, sender_phone=SENDER, sender_label="manager", text="haan"
        )
    assert done is not None and "ban gaya" in done
    order = await _order_for_customer()
    assert order is not None
    assert float(order.total_amount) == 80.0
    assert float(order.amount_paid) == 20.0
    assert SENDER not in _PENDING
    # customer got the order_confirmed notification via the patched sender
    assert any("mil gaya" in (c["text"] or "") for c in sent)


async def test_cancel_discards_draft(monkeypatch) -> None:
    _patch_extract(
        monkeypatch,
        _extract_result(
            action="new_bill",
            customer_phone="9999900124",
            items=[{"service": SERVICE, "garment": "Kurta", "qty": 1}],
        ),
    )
    async with async_session_factory() as db:
        await handle_staff_message(
            db, sender_phone=SENDER, sender_label="manager", text="1 kurta"
        )
        reply = await handle_staff_message(
            db, sender_phone=SENDER, sender_label="manager", text="nahi"
        )
    assert "cancel" in reply.lower()
    assert SENDER not in _PENDING
    assert await _order_for_customer() is None


async def test_confirm_without_phone_keeps_draft(monkeypatch) -> None:
    _patch_extract(
        monkeypatch,
        _extract_result(
            action="new_bill",
            customer_name="Verma",
            items=[{"service": SERVICE, "garment": "Kurta", "qty": 1}],
        ),
    )
    async with async_session_factory() as db:
        draft_reply = await handle_staff_message(
            db, sender_phone=SENDER, sender_label="manager", text="Verma 1 kurta"
        )
        assert "number nahi mila" in draft_reply
        blocked = await handle_staff_message(
            db, sender_phone=SENDER, sender_label="manager", text="haan"
        )
    assert "number nahi mila" in blocked
    assert SENDER in _PENDING  # draft survives until a phone arrives


async def test_unknown_item_not_priced(monkeypatch) -> None:
    _patch_extract(
        monkeypatch,
        _extract_result(
            action="new_bill",
            customer_phone="9999900124",
            items=[{"service": "Alien Service", "garment": "Spacesuit", "qty": 1}],
        ),
    )
    async with async_session_factory() as db:
        reply = await handle_staff_message(
            db, sender_phone=SENDER, sender_label="manager", text="1 spacesuit"
        )
    assert "rate card mein nahi" in reply
    assert "Total: ₹0" in reply


async def test_delay_update_writes_db_first_and_hides_reason(monkeypatch, sent) -> None:
    async with async_session_factory() as db:
        order = await create_order(
            db,
            customer_phone=CUST_PHONE,
            items=[{"type": "Kurta", "qty": 1}],
            created_by="test",
        )
        number = order.order_number
    new_date = (date.today() + timedelta(days=3)).isoformat()
    _patch_extract(
        monkeypatch,
        _extract_result(
            action="delay_update",
            order_number=number,
            new_date=new_date,
            reason="paani nahi aaya",
        ),
    )
    async with async_session_factory() as db:
        reply = await handle_staff_message(
            db, sender_phone=SENDER, sender_label="Ravi", text=f"{number} kal nahi hoga"
        )
    assert reply.startswith("✅")
    async with async_session_factory() as s:
        fresh = (
            await s.execute(select(Order).where(Order.order_number == number))
        ).scalar_one()
        assert fresh.expected_delivery == date.fromisoformat(new_date)
        assert "paani nahi aaya" in (fresh.notes or "")
    # customer messages must NEVER contain the internal reason
    for call in sent:
        assert "paani" not in (call["text"] or "")


async def test_status_update_respects_state_machine(monkeypatch, sent) -> None:
    async with async_session_factory() as db:
        order = await create_order(
            db,
            customer_phone=CUST_PHONE,
            items=[{"type": "Kurta", "qty": 1}],
            created_by="test",
        )
        number = order.order_number
    _patch_extract(
        monkeypatch,
        _extract_result(action="status_update", order_number=number, new_status="READY"),
    )
    async with async_session_factory() as db:
        reply = await handle_staff_message(
            db, sender_phone=SENDER, sender_label="Ravi", text=f"{number} ready hai"
        )
    assert "READY" in reply
    async with async_session_factory() as s:
        fresh = (
            await s.execute(select(Order).where(Order.order_number == number))
        ).scalar_one()
        assert fresh.status is OrderStatus.READY

    # backward move -> polite refusal, no change
    _patch_extract(
        monkeypatch,
        _extract_result(action="status_update", order_number=number, new_status="IN_WASH"),
    )
    async with async_session_factory() as db:
        reply = await handle_staff_message(
            db, sender_phone=SENDER, sender_label="Ravi", text=f"{number} dhulai me"
        )
    assert "allowed nahi" in reply


async def test_cancel_via_whatsapp_refused(monkeypatch) -> None:
    _patch_extract(
        monkeypatch,
        _extract_result(
            action="status_update", order_number="KK-20260101-01", new_status="CANCELLED"
        ),
    )
    async with async_session_factory() as db:
        reply = await handle_staff_message(
            db, sender_phone=SENDER, sender_label="manager", text="cancel kar do"
        )
    assert "dashboard" in reply


async def test_other_quiet_for_staff_loud_for_manager(monkeypatch) -> None:
    _patch_extract(monkeypatch, _extract_result(action="other"))
    async with async_session_factory() as db:
        assert (
            await handle_staff_message(
                db, sender_phone=SENDER, sender_label="Ravi", text="thik hai bhaiya"
            )
            is None
        )
        assert (
            await handle_staff_message(
                db, sender_phone=SENDER, sender_label="manager", text="thik hai"
            )
            is not None
        )


@pytest.fixture
async def no_task_residue():
    """Tasks created by a test must not pile up in the dev database."""
    from datetime import datetime, timezone

    from sqlalchemy import delete as _delete

    from app.models import Task

    t0 = datetime.now(timezone.utc)
    yield
    async with async_session_factory() as s:
        await s.execute(_delete(Task).where(Task.created_at >= t0))
        await s.commit()


async def test_relay_to_known_staff_becomes_a_tracked_task(
    monkeypatch, no_task_residue
) -> None:
    """Work handed to staff is tracked, not just forwarded and forgotten."""
    import app.services.tasks as tasks_module

    calls: list[dict] = []

    async def fake_send(db, *, to_phone, text=None, **kw):
        calls.append({"to": to_phone, "text": text})
        return "wamid.RELAY"

    monkeypatch.setattr(tasks_module, "send_message", fake_send)
    _patch_extract(
        monkeypatch,
        _extract_result(
            action="relay", relay_to="Ravi", relay_message="naya order aya hai, ready ho jao"
        ),
    )
    async with async_session_factory() as db:
        reply = await handle_staff_message(
            db, sender_phone=SENDER, sender_label="manager", text="Ravi ko bata do order aya"
        )
    assert reply.startswith("✅") and "Ravi" in reply
    assert "T-" in reply, "the owner gets a code he can follow up on"
    assert calls and calls[0]["to"] == "+918707093136"  # Ravi's seeded number
    assert "naya order aya hai" in calls[0]["text"]
    assert "done T-" in calls[0]["text"], "Ravi must know how to close it"


async def test_relay_unknown_target_lists_staff(monkeypatch) -> None:
    async def fake_send(db, **kw):
        raise AssertionError("must not send")

    monkeypatch.setattr(bill_module, "send_message", fake_send)
    _patch_extract(
        monkeypatch,
        _extract_result(action="relay", relay_to="Chintu", relay_message="kuch bhi"),
    )
    async with async_session_factory() as db:
        reply = await handle_staff_message(
            db, sender_phone=SENDER, sender_label="manager", text="Chintu ko bolo"
        )
    assert "nahi mila" in reply and "Ravi" in reply


async def test_task_window_closed_falls_back_to_template(
    monkeypatch, no_task_residue
) -> None:
    """Window shut -> WhatsApp forbids free-form, so the task rides an
    approved template instead of silently never arriving."""
    import app.services.tasks as tasks_module
    from app.services.whatsapp import WindowClosedError

    calls: list[dict] = []

    async def fake_send(db, *, to_phone, text=None, template_name=None, template_params=None, **kw):
        calls.append({"template_name": template_name, "template_params": template_params})
        if template_name is None:
            raise WindowClosedError("24h window closed")
        return "wamid.TPL"

    monkeypatch.setattr(tasks_module, "send_message", fake_send)
    _patch_extract(
        monkeypatch,
        _extract_result(action="relay", relay_to="Ravi", relay_message="jaldi\naao bhai"),
    )
    async with async_session_factory() as db:
        reply = await handle_staff_message(
            db, sender_phone=SENDER, sender_label="manager", text="Ravi ko bolo jaldi aao"
        )
    assert reply.startswith("✅"), "it did go out, just via a template"
    assert calls[1]["template_name"] == "kk_staff_alert"
    # Meta rejects newlines inside template params
    assert "\n" not in calls[1]["template_params"][0]
    assert "T-" in calls[1]["template_params"][0], "the code must survive the template"


async def test_task_totally_unreachable_is_reported_honestly(
    monkeypatch, no_task_residue
) -> None:
    import app.services.tasks as tasks_module
    from app.services.whatsapp import SendError, WindowClosedError

    async def fake_send(db, *, template_name=None, **kw):
        if template_name is None:
            raise WindowClosedError("24h window closed")
        raise SendError("template not approved yet")

    monkeypatch.setattr(tasks_module, "send_message", fake_send)
    _patch_extract(
        monkeypatch,
        _extract_result(action="relay", relay_to="Ravi", relay_message="jaldi aao"),
    )
    async with async_session_factory() as db:
        reply = await handle_staff_message(
            db, sender_phone=SENDER, sender_label="manager", text="Ravi ko bolo jaldi aao"
        )
    # never claim it was delivered when it wasn't — but the task is saved
    assert "⚠️" in reply and "nahi ja paya" in reply
    assert "T-" in reply


def _photo_result(**overrides) -> dict:
    """What the vision model returns: a transcription, not a bill."""
    base = {
        "readable": True,
        "customer_name": "",
        "customer_phone": "",
        "advance": 0,
        "expected_delivery": "",
        "lines": [],
        "unreadable_note": "",
    }
    base.update(overrides)
    return base


def _line(**overrides) -> dict:
    base = {"text": "", "garment": "", "service": "", "qty": 1, "unsure": False}
    base.update(overrides)
    return base


def _with_photo(monkeypatch, result: dict, seen: dict | None = None):
    """Put a fake slip on disk and mock the vision call. Returns the path."""
    media_dir = bill_module._MEDIA_DIR
    media_dir.mkdir(exist_ok=True)
    photo = media_dir / "test-billphoto.jpg"
    photo.write_bytes(b"fake-jpg")

    async def fake_vision(**kw):
        if seen is not None:
            seen.update(kw)
        return result

    monkeypatch.setattr(bill_module.llm_client, "ask_json_image", fake_vision)
    return photo


async def test_bill_from_photo(monkeypatch) -> None:
    seen: dict = {}
    photo = _with_photo(
        monkeypatch,
        _photo_result(
            customer_name="Photo Grahak",
            customer_phone="9999900124",
            lines=[_line(text="3 kurta", garment="Kurta", service=SERVICE, qty=3)],
        ),
        seen,
    )
    try:
        async with async_session_factory() as db:
            reply = await handle_staff_message(
                db, sender_phone=SENDER, sender_label="manager",
                text="[image:/admin/media/test-billphoto.jpg] Bill bnao iska",
            )
    finally:
        photo.unlink(missing_ok=True)

    assert reply is not None and "₹120" in reply and "Photo Grahak" in reply
    assert seen["image_bytes"] == b"fake-jpg"
    assert "Bill bnao iska" in seen["user_text"]
    assert SENDER in _PENDING  # confirm loop still required
    # The rate card must NOT be in the reader's context — that is what made
    # it "recognise" shirt/pant on slips it could not actually read.
    assert SERVICE not in seen["user_text"] and SERVICE not in seen["system"]


async def test_photo_unreadable_asks_instead_of_inventing(monkeypatch) -> None:
    photo = _with_photo(
        monkeypatch,
        _photo_result(readable=False, unreadable_note="photo dhundhli hai"),
    )
    try:
        async with async_session_factory() as db:
            reply = await handle_staff_message(
                db, sender_phone=SENDER, sender_label="manager",
                text="[image:/admin/media/test-billphoto.jpg] bill bana do",
            )
    finally:
        photo.unlink(missing_ok=True)

    assert reply is not None and "saaf nahi" in reply and "dhundhli" in reply
    assert SENDER not in _PENDING  # nothing invented, nothing staged


async def test_photo_unknown_garment_is_flagged_not_swapped(monkeypatch) -> None:
    """An item that isn't on the rate card keeps the slip's own word."""
    photo = _with_photo(
        monkeypatch,
        _photo_result(
            customer_name="Verma ji",
            customer_phone="9999900124",
            lines=[
                _line(text="2 topi", garment="Topi", qty=2, unsure=True),
                _line(text="1 sherwanis", garment="Sherwanis", qty=1),
            ],
        ),
    )
    try:
        async with async_session_factory() as db:
            reply = await handle_staff_message(
                db, sender_phone=SENDER, sender_label="manager",
                text="[image:/admin/media/test-billphoto.jpg]",
            )
    finally:
        photo.unlink(missing_ok=True)

    assert reply is not None
    # unknown word survives as written, marked — never turned into a shirt
    assert "Topi" in reply and "⚠️" in reply and "❓" in reply
    assert "Shirt" not in reply and "Pant" not in reply
    # a plural spelling still finds its single rate-card row
    assert "Sherwani" in reply and "₹300" in reply
    draft = _PENDING[SENDER].draft
    assert [i["garment"] for i in draft["items"]] == ["Topi", "Sherwani"]
    assert draft["total"] == 300                 # unpriced item adds nothing


async def test_photo_ambiguous_service_asks_which_one(monkeypatch) -> None:
    """Kurta exists under several services — ask, don't pick one."""
    photo = _with_photo(
        monkeypatch,
        _photo_result(
            customer_phone="9999900124",
            lines=[_line(text="2 kurta", garment="Kurta", qty=2)],
        ),
    )
    try:
        async with async_session_factory() as db:
            reply = await handle_staff_message(
                db, sender_phone=SENDER, sender_label="manager",
                text="[image:/admin/media/test-billphoto.jpg]",
            )
    finally:
        photo.unlink(missing_ok=True)

    assert reply is not None and "kaunsi service" in reply
    assert "Dry Clean" in reply and SERVICE in reply
    assert _PENDING[SENDER].draft["total"] == 0  # never guesses a price


def test_match_key_normalises_spelling_not_meaning() -> None:
    k = bill_module._key
    assert k("T-Shirt") == k("t shirt") == k("tshirts")
    assert k("Pents") == k("pant") == k("PAINT")
    assert k("Dry Clean") == k("dryclean")
    assert k("Shirt") != k("Kurta")   # different garments never collapse


async def test_photo_missing_file_stays_silent(monkeypatch) -> None:
    async def fake_vision(**kw):
        raise AssertionError("must not be called for a missing file")

    monkeypatch.setattr(bill_module.llm_client, "ask_json_image", fake_vision)
    async with async_session_factory() as db:
        reply = await handle_staff_message(
            db, sender_phone=SENDER, sender_label="manager",
            text="[image:/admin/media/does-not-exist.jpg] Bill bnao",
        )
    assert reply is None


async def test_manager_business_query_answers_from_db_facts(monkeypatch, sent) -> None:
    async with async_session_factory() as db:
        order = await create_order(
            db,
            customer_phone=CUST_PHONE,
            items=[{"type": "Kurta", "qty": 1}],
            total_amount=100,
            created_by="test",
        )
        number = order.order_number

    _patch_extract(monkeypatch, _extract_result(action="other"))
    seen: dict = {}

    async def fake_ask(**kw):
        seen.update(kw)
        return "Abhi 1 order pending hai, ₹100 baaki. — assistant"

    monkeypatch.setattr(bill_module.llm_client, "ask", fake_ask)
    async with async_session_factory() as db:
        reply = await handle_staff_message(
            db, sender_phone=SENDER, sender_label="manager", text="kitne order pending hai?"
        )
    assert reply == "Abhi 1 order pending hai, ₹100 baaki. — assistant"
    # the model only saw OUR numbers: live order + aggregates in the facts
    assert number in seen["user_text"]
    assert "Pending (active) orders" in seen["user_text"]
    assert "kitne order pending hai?" in seen["user_text"]


async def test_whatsapp_sandbox_train_loop(monkeypatch) -> None:
    """test customer -> sandboxed answer -> sikhao: -> Correction saved."""
    from app.models import Correction, Customer as Cust
    from tests.conftest import purge_phones

    bill_module._TEST_MODE.clear()
    async with async_session_factory() as db:
        db.add(Cust(phone=SENDER, name="Malik"))
        await db.commit()

    async def fake_brain(db, customer, text, sandbox=False):
        assert sandbox is True
        return "Hum sirf kapde dhote hain ji."

    monkeypatch.setattr("app.services.ai_agent.build_ai_reply", fake_brain)
    try:
        async with async_session_factory() as db:
            r1 = await handle_staff_message(
                db, sender_phone=SENDER, sender_label="manager", text="test customer"
            )
            assert "Test mode ON" in r1
            r2 = await handle_staff_message(
                db, sender_phone=SENDER, sender_label="manager", text="juta saaf karte ho?"
            )
            assert r2.startswith("🧪") and "kapde dhote" in r2
            r3 = await handle_staff_message(
                db, sender_phone=SENDER, sender_label="manager",
                text="sikhao: Haan ji, shoe cleaning bhi hoti hai, ₹150 per pair",
            )
            assert "Seekh liya" in r3
            corr = (
                await db.execute(
                    select(Correction).where(Correction.question == "juta saaf karte ho?")
                )
            ).scalar_one()
            assert "shoe cleaning" in corr.correct_reply
            r4 = await handle_staff_message(
                db, sender_phone=SENDER, sender_label="manager", text="test band"
            )
            assert "OFF" in r4
    finally:
        bill_module._TEST_MODE.clear()
        async with async_session_factory() as s:
            await s.execute(
                sqltext("DELETE FROM corrections WHERE question = 'juta saaf karte ho?'")
            )
            await s.commit()
        await purge_phones(SENDER)


async def test_llm_down_notifies_manager_but_not_staff(monkeypatch) -> None:
    async def fake_ask_json(**kw):
        raise LLMUnavailable("down")

    monkeypatch.setattr(bill_module.llm_client, "ask_json", fake_ask_json)
    async with async_session_factory() as db:
        manager_reply = await handle_staff_message(
            db, sender_phone=SENDER, sender_label="manager", text="Sharma 2 kurta"
        )
        staff_reply = await handle_staff_message(
            db, sender_phone=SENDER, sender_label="Ravi", text="Sharma 2 kurta"
        )
    assert manager_reply is not None and "uplabdh nahi" in manager_reply
    assert staff_reply is None
