"""Bill-by-text agent tests — LLM mocked; DB, pricing and confirm loop real."""

from datetime import date, timedelta

import pytest
from sqlalchemy import select, text as sqltext

import app.services.bill_agent as bill_module
from app.config import settings as settings_module
from app.database import async_session_factory
from app.models import Customer, Order, OrderStatus, Rate
from app.services.bill_agent import _PENDING, handle_staff_message
from app.services.llm_client import LLMUnavailable
from app.services.order_service import create_order
from tests.conftest import TEST_WASHER_NAME, TEST_WASHER_PHONE

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
    monkeypatch, no_task_residue, test_washer
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
            action="relay",
            relay_to=TEST_WASHER_NAME,
            relay_message="naya order aya hai, ready ho jao",
        ),
    )
    async with async_session_factory() as db:
        reply = await handle_staff_message(
            db,
            sender_phone=SENDER,
            sender_label="manager",
            text=f"{TEST_WASHER_NAME} ko bata do order aya",
        )
    assert reply.startswith("✅") and TEST_WASHER_NAME in reply
    assert "T-" in reply, "the owner gets a code he can follow up on"
    assert calls and calls[0]["to"] == TEST_WASHER_PHONE
    assert "naya order aya hai" in calls[0]["text"]
    assert "done T-" in calls[0]["text"], "the assignee must know how to close it"


async def test_relay_unknown_target_lists_staff(monkeypatch, test_washer) -> None:
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
    # reply lists whoever is actually on staff — our own washer proves the list
    assert "nahi mila" in reply and TEST_WASHER_NAME in reply


async def test_task_window_closed_falls_back_to_template(
    monkeypatch, no_task_residue, test_washer
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
        _extract_result(
            action="relay", relay_to=TEST_WASHER_NAME, relay_message="jaldi\naao bhai"
        ),
    )
    async with async_session_factory() as db:
        reply = await handle_staff_message(
            db,
            sender_phone=SENDER,
            sender_label="manager",
            text=f"{TEST_WASHER_NAME} ko bolo jaldi aao",
        )
    assert reply.startswith("✅"), "it did go out, just via a template"
    assert calls[1]["template_name"] == "kk_staff_alert"
    # Meta rejects newlines inside template params
    assert "\n" not in calls[1]["template_params"][0]
    assert "T-" in calls[1]["template_params"][0], "the code must survive the template"


async def test_task_totally_unreachable_is_reported_honestly(
    monkeypatch, no_task_residue, test_washer
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
        _extract_result(action="relay", relay_to=TEST_WASHER_NAME, relay_message="jaldi aao"),
    )
    async with async_session_factory() as db:
        reply = await handle_staff_message(
            db,
            sender_phone=SENDER,
            sender_label="manager",
            text=f"{TEST_WASHER_NAME} ko bolo jaldi aao",
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


# --- "order details do" — staff apna kaam poochh raha hai ------------------


async def test_staff_asking_for_work_gets_a_tappable_list(monkeypatch, sent, test_washer) -> None:
    """Pehle staff ka ye sawal CHUP-CHAAP gir jata tha.

    Ab jawab list ban kar jata hai — har order/task apni line par, taaki
    "kis par jawab diya" ka sawal hi na bache. Sab DB se, koi LLM nahi.
    """
    async def no_llm(**kw):
        raise AssertionError("ye jawab bina LLM ke aana chahiye")

    monkeypatch.setattr(bill_module.llm_client, "ask_json", no_llm)
    async with async_session_factory() as db:
        order = await create_order(
            db, customer_phone=CUST_PHONE, customer_name="Detail Grahak",
            items=[{"type": "Kurta", "qty": 2}], created_by="test",
        )
        number = order.order_number
    sent.clear()   # order banne par customer ko gaya confirmation — wo setup hai

    for asked in ("order details do", "aj k orders details do", "aaj ka kaam kya hai"):
        sent.clear()
        async with async_session_factory() as db:
            reply = await handle_staff_message(
                db, sender_phone=TEST_WASHER_PHONE,
                sender_label=TEST_WASHER_NAME, text=asked,
            )
        assert reply == "", "list khud chali jati hai, webhook dobara na bheje"
        assert len(sent) == 1 and sent[0]["to"] == TEST_WASHER_PHONE
        rows = sent[0].get("list_rows") or []
        assert any(r.id == f"pick:o:{number}" for r in rows), f"{asked!r} par order dikhna chahiye"


async def test_worklist_falls_back_to_text_when_list_cannot_go(
    monkeypatch, sent, test_washer
) -> None:
    """Window band ho to bhi kaam ki list milni chahiye — bina buttons ke."""
    from app.services.whatsapp import WindowClosedError

    async def no_llm(**kw):
        raise AssertionError("ye jawab bina LLM ke aana chahiye")

    monkeypatch.setattr(bill_module.llm_client, "ask_json", no_llm)

    async def closed(db, **kw):
        raise WindowClosedError("24h window closed")

    async with async_session_factory() as db:
        order = await create_order(
            db, customer_phone=CUST_PHONE, items=[{"type": "Kurta", "qty": 1}],
            created_by="test",
        )
        number = order.order_number
    monkeypatch.setattr(bill_module, "send_message", closed)
    async with async_session_factory() as db:
        reply = await handle_staff_message(
            db, sender_phone=TEST_WASHER_PHONE,
            sender_label=TEST_WASHER_NAME, text="order details do",
        )
    assert reply and number in reply and "done KK-" in reply


async def test_tapping_a_row_opens_that_one_with_buttons(monkeypatch, sent, test_washer) -> None:
    """List se chuna hua order -> uska apna card + wahi teen buttons."""
    async def no_llm(**kw):
        raise AssertionError("button ke jawab mein LLM nahi chalna chahiye")

    monkeypatch.setattr(bill_module.llm_client, "ask_json", no_llm)
    async with async_session_factory() as db:
        order = await create_order(
            db, customer_phone=CUST_PHONE, customer_name="Tap Grahak",
            items=[{"type": "Kurta", "qty": 1}], created_by="test",
        )
        number = order.order_number
    sent.clear()

    async with async_session_factory() as db:
        reply = await handle_staff_message(
            db, sender_phone=TEST_WASHER_PHONE, sender_label=TEST_WASHER_NAME,
            text=f"[button:pick:o:{number}] {number}",
        )
    assert reply == ""
    card = next(c for c in sent if c["to"] == TEST_WASHER_PHONE)
    assert number in card["text"] and "Tap Grahak" in card["text"]
    ids = [b.id for b in (card.get("buttons") or [])]
    assert ids == [f"ord:{number}:done", f"ord:{number}:later", f"ord:{number}:problem"]


async def test_order_button_done_moves_the_order(monkeypatch, sent, test_washer) -> None:
    """✅ dabate hi wahi hota hai jo 'done KK-...' likhne par hota hai."""
    async def no_llm(**kw):
        raise AssertionError("button ke jawab mein LLM nahi chalna chahiye")

    monkeypatch.setattr(bill_module.llm_client, "ask_json", no_llm)
    async with async_session_factory() as db:
        order = await create_order(
            db, customer_phone=CUST_PHONE, items=[{"type": "Kurta", "qty": 1}],
            created_by="test",
        )
        number = order.order_number

    async with async_session_factory() as db:
        reply = await handle_staff_message(
            db, sender_phone=TEST_WASHER_PHONE, sender_label=TEST_WASHER_NAME,
            text=f"[button:ord:{number}:done] ✅ Ho gaya",
        )
    assert reply and number in reply
    async with async_session_factory() as s:
        fresh = (
            await s.execute(select(Order).where(Order.order_number == number))
        ).scalar_one()
        assert fresh.status is OrderStatus.READY, "washer ke liye 'ho gaya' = READY"


async def test_order_button_later_records_the_eta(monkeypatch, sent, test_washer) -> None:
    """⏳ ke baad ka jawab order par likha jaye aur owner tak pahunche."""
    async def no_llm(**kw):
        raise AssertionError("button ke jawab mein LLM nahi chalna chahiye")

    monkeypatch.setattr(bill_module.llm_client, "ask_json", no_llm)
    async with async_session_factory() as db:
        order = await create_order(
            db, customer_phone=CUST_PHONE, items=[{"type": "Kurta", "qty": 1}],
            created_by="test",
        )
        number = order.order_number

    async with async_session_factory() as db:
        ask = await handle_staff_message(
            db, sender_phone=TEST_WASHER_PHONE, sender_label=TEST_WASHER_NAME,
            text=f"[button:ord:{number}:later] ⏳ Time lagega",
        )
    assert ask and "kab tak" in ask.lower()

    sent.clear()
    async with async_session_factory() as db:
        ack = await handle_staff_message(
            db, sender_phone=TEST_WASHER_PHONE, sender_label=TEST_WASHER_NAME,
            text="sham tak ho jayega",
        )
    assert ack and "sham tak" in ack
    async with async_session_factory() as s:
        fresh = (
            await s.execute(select(Order).where(Order.order_number == number))
        ).scalar_one()
        assert "sham tak" in (fresh.notes or ""), "ETA order par likhi jaye"
    assert any("sham tak" in (c.get("text") or "") for c in sent), "owner ko khabar jaye"


async def test_worklist_stays_out_of_the_way(monkeypatch, sent, test_washer) -> None:
    """Rozmarra ki baat-cheet par ye handler nahi jagna chahiye."""
    seen: list[str] = []

    async def fake_ask_json(**kw):
        seen.append("llm")
        return _extract_result()

    monkeypatch.setattr(bill_module.llm_client, "ask_json", fake_ask_json)
    for chatter in ("thik hai bhaiya", "haan", "sham tak ho jayega", "Monday"):
        async with async_session_factory() as db:
            await handle_staff_message(
                db, sender_phone=TEST_WASHER_PHONE,
                sender_label=TEST_WASHER_NAME, text=chatter,
            )
    assert seen, "normal baat abhi bhi purane raste se jati hai"


async def test_deactivated_staff_gets_no_worklist(monkeypatch, sent, test_washer) -> None:
    """Band kiye gaye aadmi ko shop ka kaam nahi dikhna chahiye."""
    from app.models import Staff

    async with async_session_factory() as db:
        st = await db.get(Staff, test_washer)
        st.is_active = False
        await db.commit()

    async def no_llm(**kw):
        raise AssertionError("band staff par LLM bhi nahi chalna chahiye")

    monkeypatch.setattr(bill_module.llm_client, "ask_json", no_llm)
    async with async_session_factory() as db:
        reply = await handle_staff_message(
            db, sender_phone=TEST_WASHER_PHONE,
            sender_label=TEST_WASHER_NAME, text="order details do",
        )
    assert reply is None


# --- staff ne order par dikkat batayi -------------------------------------


async def test_damaged_garment_alerts_the_owner_not_the_customer(
    monkeypatch, sent, test_washer
) -> None:
    """Asli ghatna (09 Aug): Ravi ne likha "lehenga khrab hai, service nahi
    hogi". Wo sirf ek note ban kar order par baith gaya — owner ko kabhi
    pata hi nahi chala, aur customer ko 'ready' ka message pehle hi ja
    chuka tha. Ab: owner ko turant, customer ko kuch nahi.
    """
    async def no_llm(**kw):
        raise AssertionError("dikkat pakadne ke liye LLM par bharosa nahi")

    monkeypatch.setattr(bill_module.llm_client, "ask_json", no_llm)
    async with async_session_factory() as db:
        order = await create_order(
            db, customer_phone=CUST_PHONE, customer_name="Pooja",
            items=[{"type": "Lehenga", "qty": 1}], created_by="test",
        )
        number = order.order_number
    sent.clear()

    bare_ref = number.replace("KK-", "")
    async with async_session_factory() as db:
        reply = await handle_staff_message(
            db, sender_phone=TEST_WASHER_PHONE, sender_label=TEST_WASHER_NAME,
            text=f"{bare_ref} isme ek lahenga hai uska service nhi hog kyoki wo khrab hai",
        )
    assert reply and "bata di" in reply
    # customer ko kuch nahi
    assert not [c for c in sent if c["to"] == CUST_PHONE], \
        "kapda kharab hona owner ka faisla hai, bot customer ko na bole"
    # owner ko poori baat, ek tap ke faisle ke saath
    owner_msg = next(c for c in sent if number in (c.get("text") or ""))
    assert "khrab" in owner_msg["text"] and TEST_WASHER_NAME in owner_msg["text"]
    assert [b.id for b in (owner_msg.get("buttons") or [])] == [
        f"hold:{number}:yes", f"hold:{number}:no",
    ]
    async with async_session_factory() as s:
        fresh = (
            await s.execute(select(Order).where(Order.order_number == number))
        ).scalar_one()
        assert "khrab" in (fresh.notes or ""), "baat order par likhi rahe"
        assert fresh.status is OrderStatus.RECEIVED, "status apne aap na badle"


async def test_owner_is_warned_when_the_customer_already_heard_ready(
    monkeypatch, sent, test_washer
) -> None:
    from app.services.order_service import update_status

    async def no_llm(**kw):
        raise AssertionError("dikkat pakadne ke liye LLM par bharosa nahi")

    monkeypatch.setattr(bill_module.llm_client, "ask_json", no_llm)
    async with async_session_factory() as db:
        order = await create_order(
            db, customer_phone=CUST_PHONE, customer_name="Pooja",
            items=[{"type": "Lehenga", "qty": 1}], created_by="test",
        )
        await update_status(db, order, OrderStatus.READY, changed_by="test")
        number = order.order_number
    sent.clear()

    async with async_session_factory() as db:
        await handle_staff_message(
            db, sender_phone=TEST_WASHER_PHONE, sender_label=TEST_WASHER_NAME,
            text=f"{number} ka lehenga phat gaya hai",
        )
    owner_msg = next(c for c in sent if number in (c.get("text") or ""))
    assert "ja chuka hai" in owner_msg["text"], \
        "owner ko pata hona chahiye ki customer ko ready bola ja chuka hai"


async def test_owner_can_hold_the_order_with_one_tap(monkeypatch, sent, test_washer) -> None:
    """🛑 dabate hi order ruke aur uska peechha karna band ho."""
    from app.models import Staff
    from app.services import tasks as task_service

    async def no_llm(**kw):
        raise AssertionError("button ke jawab mein LLM nahi chahiye")

    monkeypatch.setattr(bill_module.llm_client, "ask_json", no_llm)
    async with async_session_factory() as db:
        order = await create_order(
            db, customer_phone=CUST_PHONE, items=[{"type": "Lehenga", "qty": 1}],
            created_by="test",
        )
        number = order.order_number
        st = await db.get(Staff, test_washer)
        await task_service.create_task(
            db, title="is order ka kaam", staff=st, order=order, notify=False,
        )
    sent.clear()

    async with async_session_factory() as db:
        reply = await handle_staff_message(
            db, sender_phone=settings_module.MANAGER_PHONE, sender_label="manager",
            text=f"[button:hold:{number}:yes] 🛑 Order rok do",
        )
    assert reply and "hold" in reply.lower()
    async with async_session_factory() as s:
        fresh = (
            await s.execute(select(Order).where(Order.order_number == number))
        ).scalar_one()
        assert fresh.status is OrderStatus.ON_HOLD
        open_left = (
            await s.execute(
                sqltext(
                    "SELECT count(*) FROM tasks WHERE order_id = :o AND status = 'OPEN'"
                ),
                {"o": str(fresh.id)},
            )
        ).scalar_one()
        assert open_left == 0, "ruke order ka peechha karna band hona chahiye"
    # customer ko hold ki koi khabar nahi jati
    assert not [c for c in sent if c["to"] == CUST_PHONE]


async def test_owner_can_let_it_run(monkeypatch, sent) -> None:
    async with async_session_factory() as db:
        order = await create_order(
            db, customer_phone=CUST_PHONE, items=[{"type": "Kurta", "qty": 1}],
            created_by="test",
        )
        number = order.order_number
    async with async_session_factory() as db:
        reply = await handle_staff_message(
            db, sender_phone=settings_module.MANAGER_PHONE, sender_label="manager",
            text=f"[button:hold:{number}:no] ▶️ Chalne do",
        )
    assert reply and "chalta rahega" in reply
    async with async_session_factory() as s:
        fresh = (
            await s.execute(select(Order).where(Order.order_number == number))
        ).scalar_one()
        assert fresh.status is OrderStatus.RECEIVED


async def test_ordinary_chatter_is_not_treated_as_a_problem(
    monkeypatch, sent, test_washer
) -> None:
    """Bina order ke shikayat, ya order ke saath saadi baat — dono par ye
    handler na jage; warna har baat owner ko alert ban jayegi."""
    seen: list[str] = []

    async def fake_ask_json(**kw):
        seen.append("llm")
        return _extract_result()

    monkeypatch.setattr(bill_module.llm_client, "ask_json", fake_ask_json)
    async with async_session_factory() as db:
        order = await create_order(
            db, customer_phone=CUST_PHONE, items=[{"type": "Kurta", "qty": 1}],
            created_by="test",
        )
        number = order.order_number
    for chatter in ("machine kharab hai", f"{number} kal dunga", "thik hai bhaiya"):
        sent.clear()
        async with async_session_factory() as db:
            await handle_staff_message(
                db, sender_phone=TEST_WASHER_PHONE,
                sender_label=TEST_WASHER_NAME, text=chatter,
            )
        assert not [c for c in sent if "🚨" in (c.get("text") or "")], \
            f"{chatter!r} par owner ko alert nahi jana chahiye"


# --- relay: naam bhi sender ka, baat bhi sender ki -------------------------


async def test_relay_never_guesses_who(monkeypatch, sent, test_washer) -> None:
    """Asli galti (09 Aug): owner ne likha "Message bhejo message kyo nhi
    bheje" — kisi ka naam tha hi nahi — aur bot ne Ravi ko bhej diya.
    Ab jis naam ko sender ne likha hi nahi, uske paas kuch nahi jata.
    """
    _patch_extract(
        monkeypatch,
        _extract_result(
            action="relay", relay_to=TEST_WASHER_NAME,
            relay_message="Message kyun nahi bheje?",
        ),
    )
    async with async_session_factory() as db:
        reply = await handle_staff_message(
            db, sender_phone=SENDER, sender_label="manager",
            text="Message bhejo message kyo nhi bheje",
        )
    assert reply and "Kisko bhejun" in reply
    assert TEST_WASHER_NAME in reply, "staff ke naam dikhne chahiye"
    assert not [c for c in sent if c["to"] == TEST_WASHER_PHONE], "kisi ko kuch na jaye"


async def test_relay_without_a_message_asks_instead_of_inventing(
    monkeypatch, sent, no_task_residue, test_washer
) -> None:
    """"Ajit ko bhej do" — kise pata hai, kya nahi. Pehle model ne PICHHLA
    BOT ka jawab utha kar bhej diya tha aur uska task bhi ban gaya tha.
    """
    _patch_extract(
        monkeypatch,
        _extract_result(
            action="relay", relay_to=TEST_WASHER_NAME,
            relay_message="Note save ho gaya hai (KK-20260809-01).",
        ),
    )
    async with async_session_factory() as db:
        reply = await handle_staff_message(
            db, sender_phone=SENDER, sender_label="manager",
            text=f"{TEST_WASHER_NAME} ko bhej do",
        )
    assert reply and "kya bhejun" in reply.lower()
    assert not [c for c in sent if c["to"] == TEST_WASHER_PHONE]

    # ab asli baat likhne par wahi jaati hai — aur task ban jata hai
    sent.clear()

    async def no_llm(**kw):
        raise AssertionError("jawab pehle se pata hai, LLM ki zarurat nahi")

    monkeypatch.setattr(bill_module.llm_client, "ask_json", no_llm)
    async with async_session_factory() as db:
        done = await handle_staff_message(
            db, sender_phone=SENDER, sender_label="manager",
            text="kal subah 9 baje aa jana",
        )
    assert done and TEST_WASHER_NAME in done and "T-" in done
    got = next(c for c in sent if c["to"] == TEST_WASHER_PHONE)
    assert "kal subah 9 baje" in got["text"]


async def test_a_named_relay_still_works(monkeypatch, sent, no_task_residue, test_washer) -> None:
    """Rok-tok sirf andhere mein — naam aur baat dono ho to seedha jaye."""
    _patch_extract(
        monkeypatch,
        _extract_result(
            action="relay", relay_to=TEST_WASHER_NAME,
            relay_message="Sharma ji ka order aaj hi nikalna hai",
        ),
    )
    async with async_session_factory() as db:
        reply = await handle_staff_message(
            db, sender_phone=SENDER, sender_label="manager",
            text=f"{TEST_WASHER_NAME} ko bolo Sharma ji ka order aaj hi nikalna hai",
        )
    assert reply and reply.startswith("✅")
    assert [c for c in sent if c["to"] == TEST_WASHER_PHONE]
