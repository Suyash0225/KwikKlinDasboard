"""Webhook tests: verification handshake, signatures, storage, dedup, ack.

WhatsApp's real API is never called — the `sent` fixture records what WOULD
have been sent.
"""

import pytest
from sqlalchemy import select

from app.config import settings
from app.database import async_session_factory
from app.models import Conversation, Customer, Staff
from app.services.messages import get_message
from tests.conftest import (
    TEST_WASHER_PHONE,
    TEST_WASHER_PHONE_RAW,
    TEST_CUSTOMER_PHONE,
    TEST_CUSTOMER_PHONE_RAW,
    meta_payload,
    sign_body,
)


# --- GET verification handshake ---

async def test_verify_ok(client) -> None:
    r = await client.get(
        "/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": settings.WHATSAPP_VERIFY_TOKEN,
            "hub.challenge": "CHALLENGE-42",
        },
    )
    assert r.status_code == 200
    assert r.text == "CHALLENGE-42"


async def test_verify_wrong_token(client) -> None:
    r = await client.get(
        "/webhook",
        params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "x"},
    )
    assert r.status_code == 403


# --- signature enforcement ---

async def test_post_without_signature_rejected(client, sent) -> None:
    body = meta_payload(
        messages=[{"from": TEST_CUSTOMER_PHONE_RAW, "id": "wamid.TEST-nosig", "type": "text", "text": {"body": "hi"}}]
    )
    r = await client.post("/webhook", content=body)
    assert r.status_code == 403
    assert sent == []


async def test_post_bad_signature_rejected(client, sent) -> None:
    body = meta_payload(
        messages=[{"from": TEST_CUSTOMER_PHONE_RAW, "id": "wamid.TEST-badsig", "type": "text", "text": {"body": "hi"}}]
    )
    r = await client.post(
        "/webhook", content=body, headers={"X-Hub-Signature-256": "sha256=deadbeef"}
    )
    assert r.status_code == 403
    assert sent == []


# --- inbound customer message ---

async def test_inbound_text_stores_and_acks(client, sent) -> None:
    body = meta_payload(
        messages=[{
            "from": TEST_CUSTOMER_PHONE_RAW,
            "id": "wamid.TEST-text1",
            "type": "text",
            "text": {"body": "mera order kahan hai"},
        }]
    )
    r = await client.post("/webhook", content=body, headers={"X-Hub-Signature-256": sign_body(body)})
    assert r.status_code == 200

    async with async_session_factory() as s:
        cust = (
            await s.execute(select(Customer).where(Customer.phone == TEST_CUSTOMER_PHONE))
        ).scalar_one()
        assert cust.last_message_at is not None, "24h window did not open"
        conv = (
            await s.execute(select(Conversation).where(Conversation.wa_message_id == "wamid.TEST-text1"))
        ).scalar_one()
        assert conv.message_text == "mera order kahan hai"
        assert conv.customer_id == cust.id and conv.staff_id is None

    # exactly one ack, to the customer, with the registered string
    # (AI signature is appended deeper, inside whatsapp.send_message —
    # this fixture replaces send_message, so raw text is expected here)
    assert len(sent) == 1
    assert sent[0]["to"] == TEST_CUSTOMER_PHONE
    assert sent[0]["text"] == get_message("ack_received")


async def test_duplicate_delivery_stored_and_acked_once(client, sent) -> None:
    body = meta_payload(
        messages=[{
            "from": TEST_CUSTOMER_PHONE_RAW,
            "id": "wamid.TEST-dup",
            "type": "text",
            "text": {"body": "hello"},
        }]
    )
    headers = {"X-Hub-Signature-256": sign_body(body)}
    assert (await client.post("/webhook", content=body, headers=headers)).status_code == 200
    assert (await client.post("/webhook", content=body, headers=headers)).status_code == 200

    async with async_session_factory() as s:
        rows = (
            await s.execute(select(Conversation).where(Conversation.wa_message_id == "wamid.TEST-dup"))
        ).scalars().all()
        assert len(rows) == 1, "duplicate wamid must not double-insert"
    assert len(sent) == 1, "duplicate delivery must not double-ack"


async def test_button_reply_preserves_id(client, sent) -> None:
    body = meta_payload(
        messages=[{
            "from": TEST_CUSTOMER_PHONE_RAW,
            "id": "wamid.TEST-btn",
            "type": "interactive",
            "interactive": {
                "type": "button_reply",
                "button_reply": {"id": "order:abc123:done", "title": "Done ✅"},
            },
        }]
    )
    r = await client.post("/webhook", content=body, headers={"X-Hub-Signature-256": sign_body(body)})
    assert r.status_code == 200

    async with async_session_factory() as s:
        conv = (
            await s.execute(select(Conversation).where(Conversation.wa_message_id == "wamid.TEST-btn"))
        ).scalar_one()
        # Phase 3.5 will parse the action out of this — the id must survive.
        assert conv.message_text.startswith("[button:order:abc123:done]")


async def test_list_reply_arrives_as_a_button(client, sent) -> None:
    """List se chuni gayi line bhi button jaisi hi dikhni chahiye.

    Buttons max 3 hote hain; 5-6 kaam chunwane ke liye list hi ek rasta hai.
    Agar iska shape alag hota to har handler ko do bar likhna padta.
    """
    body = meta_payload(
        messages=[{
            "from": TEST_CUSTOMER_PHONE_RAW,
            "id": "wamid.TEST-list",
            "type": "interactive",
            "interactive": {
                "type": "list_reply",
                "list_reply": {
                    "id": "pick:o:KK-20260809-01",
                    "title": "KK-20260809-01",
                    "description": "Pooja — 1 x Lehenga",
                },
            },
        }]
    )
    r = await client.post("/webhook", content=body, headers={"X-Hub-Signature-256": sign_body(body)})
    assert r.status_code == 200
    async with async_session_factory() as s:
        conv = (
            await s.execute(select(Conversation).where(Conversation.wa_message_id == "wamid.TEST-list"))
        ).scalar_one()
        assert conv.message_text.startswith("[button:pick:o:KK-20260809-01]")


# --- staff sender ---

async def test_staff_message_goes_to_staff_row_no_ack(client, sent, test_washer) -> None:
    # Other command tests share the in-memory pending map; this neutral message
    # must not inherit a previous task/order context for the same staff phone.
    from app.services import bill_agent
    bill_agent._PENDING.pop(TEST_WASHER_PHONE, None)
    body = meta_payload(
        messages=[{
            "from": TEST_WASHER_PHONE_RAW,
            "id": "wamid.TEST-ravi",
            "type": "text",
            "text": {"body": "ok thanks"},
        }]
    )
    r = await client.post("/webhook", content=body, headers={"X-Hub-Signature-256": sign_body(body)})
    assert r.status_code == 200

    async with async_session_factory() as s:
        washer = (
            await s.execute(select(Staff).where(Staff.phone == TEST_WASHER_PHONE))
        ).scalar_one()
        assert washer.last_message_at is not None, "staff window did not open"
        conv = (
            await s.execute(select(Conversation).where(Conversation.wa_message_id == "wamid.TEST-ravi"))
        ).scalar_one()
        assert conv.staff_id == washer.id and conv.customer_id is None
        # no customer row must appear for a staff phone
        ghost = (
            await s.execute(select(Customer).where(Customer.phone == TEST_WASHER_PHONE))
        ).scalar_one_or_none()
        assert ghost is None
    assert sent == [], "staff messages must not be acked"


# --- who is this number? ---

async def _inbound_from(client, text: str, profile: str | None, wamid: str):
    msg = {
        "id": wamid, "from": TEST_CUSTOMER_PHONE_RAW, "type": "text",
        "text": {"body": text},
    }
    contacts = (
        [{"wa_id": TEST_CUSTOMER_PHONE_RAW, "profile": {"name": profile}}]
        if profile is not None else None
    )
    body = meta_payload(messages=[msg], contacts=contacts)
    r = await client.post(
        "/webhook", content=body, headers={"X-Hub-Signature-256": sign_body(body)}
    )
    assert r.status_code == 200


async def _customer():
    async with async_session_factory() as s:
        return (
            await s.execute(select(Customer).where(Customer.phone == TEST_CUSTOMER_PHONE))
        ).scalar_one_or_none()


async def test_unknown_number_is_named_from_its_whatsapp_profile(client, sent) -> None:
    """Meta introduces the sender — no reason to store a bare number."""
    await _inbound_from(client, "bhaiya kitna lagega?", "Sharma Ji", "wamid.TESTname1")
    c = await _customer()
    assert c is not None and c.name == "Sharma Ji"


async def test_profile_name_fills_a_blank_but_never_overwrites(client, sent) -> None:
    """The shop's own name for a customer always wins."""
    await _inbound_from(client, "hello", None, "wamid.TESTname2")
    assert (await _customer()).name is None

    await _inbound_from(client, "hello again", "Whatsapp Naam", "wamid.TESTname3")
    assert (await _customer()).name == "Whatsapp Naam", "khali jagah bharni chahiye"

    async with async_session_factory() as s:
        cust = (
            await s.execute(select(Customer).where(Customer.phone == TEST_CUSTOMER_PHONE))
        ).scalar_one()
        cust.name = "Dukaan Wala Naam"
        await s.commit()
    await _inbound_from(client, "aur ek", "Whatsapp Naam", "wamid.TESTname4")
    assert (await _customer()).name == "Dukaan Wala Naam", "shop ka naam nahi badalna chahiye"


async def test_a_number_is_not_a_name(client, sent) -> None:
    """Plenty of people set their own number as their WhatsApp name."""
    await _inbound_from(client, "hi", "+91 98765 43210", "wamid.TESTname5")
    assert (await _customer()).name is None


@pytest.mark.parametrize("kind", ["good", "mid", "bad"])
async def test_review_request_is_neutral_for_every_rating(monkeypatch, kind) -> None:
    """Review links must not be gated on a positive rating or offer incentives."""
    import uuid
    import app.routers.webhook as wh
    from app.database import async_session_factory
    from app.models import Customer
    from app.services import audit, customer_messages, tenant_context

    sent_messages = []

    async def fake_send(db, *, to_phone, text, **kwargs):
        sent_messages.append({"to": to_phone, "text": text})
        return "mock-wa-id"

    async def fake_audit(**kwargs):
        return None

    async def fake_links(db):
        return ["https://reviews.example/kwikklin"]

    async def fake_short(db, tenant):
        return None

    monkeypatch.setattr(wh, "send_message", fake_send)
    monkeypatch.setattr(wh, "manager_phone", lambda: "+919999000001")
    monkeypatch.setattr(tenant_context, "effective_tenant_id", lambda: None)
    monkeypatch.setattr(customer_messages, "review_links", fake_links)
    monkeypatch.setattr(customer_messages, "short_review_link", fake_short)
    monkeypatch.setattr(audit, "record", fake_audit)

    phone = f"+91999{uuid.uuid4().int % 10**7:07d}"
    async with async_session_factory() as db:
        customer = Customer(phone=phone, name="Review Test")
        db.add(customer)
        await db.flush()
        await wh._handle_rating(db, customer, phone, kind)

    customer_messages_sent = [m for m in sent_messages if m["to"] == phone]
    assert len(customer_messages_sent) == 1
    message = customer_messages_sent[0]["text"]
    assert "honest feedback" in message.lower() or "imaandaar feedback" in message.lower()
    assert "https://reviews.example/kwikklin" in message
    assert "⭐⭐⭐⭐⭐" not in message
    assert "🎁" not in message
    if kind == "bad":
        assert any(m["to"] == "+919999000001" for m in sent_messages)


@pytest.mark.parametrize(
    "stop_text",
    ["STOP", "unsubscribe", "band karo", "band kro", "msg mat bhejo", "message mat bhejo"],
)
async def test_stop_variants_set_global_and_marketing_optout(client, sent, stop_text) -> None:
    """Every supported opt-out phrase must persist before any future marketing send."""
    from app.database import async_session_factory
    from app.models import Customer

    async with async_session_factory() as db:
        customer = (
            await db.execute(select(Customer).where(Customer.phone == TEST_CUSTOMER_PHONE))
        ).scalar_one_or_none()
        if customer is not None:
            customer.opted_out = False
            customer.marketing_opt_out = False
            await db.commit()

    await _inbound_from(
        client, stop_text, None, f"wamid.TEST-stop-{abs(hash(stop_text))}"
    )

    async with async_session_factory() as db:
        customer = (
            await db.execute(select(Customer).where(Customer.phone == TEST_CUSTOMER_PHONE))
        ).scalar_one()
        assert customer.opted_out is True
        assert customer.marketing_opt_out is True


async def test_start_reenables_customer_after_stop(client, sent) -> None:
    from app.database import async_session_factory
    from app.models import Customer

    async with async_session_factory() as db:
        customer = (
            await db.execute(select(Customer).where(Customer.phone == TEST_CUSTOMER_PHONE))
        ).scalar_one_or_none()
        if customer is None:
            customer = Customer(phone=TEST_CUSTOMER_PHONE, name="Optout Test")
            db.add(customer)
        customer.opted_out = True
        customer.marketing_opt_out = True
        await db.commit()

    await _inbound_from(client, "START", None, "wamid.TEST-start-reenable")

    async with async_session_factory() as db:
        customer = (
            await db.execute(select(Customer).where(Customer.phone == TEST_CUSTOMER_PHONE))
        ).scalar_one()
        assert customer.opted_out is False
        assert customer.marketing_opt_out is False


async def test_human_takeover_grace_window_suppresses_agent_reply(monkeypatch) -> None:
    from datetime import datetime, timedelta, timezone
    import uuid
    from app.database import async_session_factory
    from app.models import Conversation, Customer, Direction
    from app.routers.webhook import _human_handoff_waiting
    from app.services import app_settings

    async def grace_setting(db, key):
        assert key == "human_handoff_grace_minutes"
        return 15

    monkeypatch.setattr(app_settings, "get", grace_setting)
    phone = f"+91998{uuid.uuid4().int % 10**7:07d}"
    async with async_session_factory() as db:
        customer = Customer(phone=phone, name="Human Takeover Test")
        db.add(customer)
        await db.flush()
        human_message = Conversation(
            customer_id=customer.id,
            direction=Direction.OUTBOUND,
            message_text="Main aapki madad karta hoon.",
            sent_by="human",
            created_at=datetime.now(timezone.utc) - timedelta(minutes=2),
        )
        inbound = Conversation(
            customer_id=customer.id,
            direction=Direction.INBOUND,
            message_text="order ka status?",
            wa_message_id=f"wamid.TEST-human-takeover-{uuid.uuid4().hex}",
        )
        db.add_all([human_message, inbound])
        await db.flush()
        assert await _human_handoff_waiting(db, customer, inbound) is True
        human_message.created_at = datetime.now(timezone.utc) - timedelta(minutes=16)
        await db.flush()
        assert await _human_handoff_waiting(db, customer, inbound) is False


# --- statuses ---

async def test_status_receipt_moves_the_ticks_forward_only(client, sent) -> None:
    """✓ sent -> ✓✓ delivered -> blue ✓✓ read, and never backwards.

    Meta can deliver these out of order; a 'delivered' arriving after 'read'
    must not un-read the message in the Inbox.
    """
    from app.models import Customer, Direction

    wamid = "wamid.TESTtick-1"
    async with async_session_factory() as s:
        cust = Customer(phone=TEST_CUSTOMER_PHONE, name="Tick Grahak")
        s.add(cust)
        await s.flush()
        s.add(
            Conversation(
                customer_id=cust.id, direction=Direction.OUTBOUND,
                message_text="aapka order taiyar hai", wa_message_id=wamid,
                sent_by="bot", status="sent",
            )
        )
        await s.commit()

    async def _post(status: str) -> None:
        body = meta_payload(
            statuses=[{"id": wamid, "status": status, "recipient_id": TEST_CUSTOMER_PHONE_RAW}]
        )
        r = await client.post(
            "/webhook", content=body, headers={"X-Hub-Signature-256": sign_body(body)}
        )
        assert r.status_code == 200

    async def _status() -> str:
        async with async_session_factory() as s:
            row = (
                await s.execute(
                    select(Conversation).where(Conversation.wa_message_id == wamid)
                )
            ).scalar_one()
            return row.status

    await _post("delivered")
    assert await _status() == "delivered"
    await _post("read")
    assert await _status() == "read"
    await _post("delivered")           # late duplicate
    assert await _status() == "read", "read message must not go back to delivered"


async def test_status_receipt_stores_nothing(client, sent) -> None:
    body = meta_payload(
        statuses=[{"id": "wamid.TEST-status", "status": "delivered", "recipient_id": TEST_CUSTOMER_PHONE_RAW}]
    )
    r = await client.post("/webhook", content=body, headers={"X-Hub-Signature-256": sign_body(body)})
    assert r.status_code == 200

    async with async_session_factory() as s:
        rows = (
            await s.execute(select(Conversation).where(Conversation.wa_message_id == "wamid.TEST-status"))
        ).scalars().all()
        assert rows == []
    assert sent == []


# --- ek baat, EK jawab (coalescing) ----------------------------------------


async def test_two_texts_in_one_batch_get_one_reply(client, sent) -> None:
    """"11 iron" + "3 dryclean" ek hi batch mein = EK jawab, do nahi.

    Pehle har message ka apna jawab jata tha — doosra jawab pehli poori
    baat dohrata tha aur customer puchta tha "ye do baar kyon bheja?"
    (Sakshi, 11 Aug). Jawab aakhri message par banta hai jo poori baat
    dekh chuka hota hai.
    """
    body = meta_payload(
        messages=[
            {
                "from": TEST_CUSTOMER_PHONE_RAW,
                "id": "wamid.TESTco1",
                "type": "text",
                "text": {"body": "11 iron"},
            },
            {
                "from": TEST_CUSTOMER_PHONE_RAW,
                "id": "wamid.TESTco2",
                "type": "text",
                "text": {"body": "3 dryclean"},
            },
        ]
    )
    r = await client.post(
        "/webhook", content=body, headers={"X-Hub-Signature-256": sign_body(body)}
    )
    assert r.status_code == 200

    # dono message store hue — coalescing sirf JAWAB rokta hai, record nahi
    async with async_session_factory() as s:
        for wamid in ("wamid.TESTco1", "wamid.TESTco2"):
            row = (
                await s.execute(
                    select(Conversation).where(Conversation.wa_message_id == wamid)
                )
            ).scalar_one()
            assert row is not None

    replies = [m for m in sent if m["to"] == TEST_CUSTOMER_PHONE]
    assert len(replies) == 1, f"ek baat par {len(replies)} jawab gaye: {replies}"


async def test_reply_superseded_by_newer_message_is_not_sent(client, sent, monkeypatch) -> None:
    """Jawab BANTE waqt (LLM ke 4-10s) naya message aa jaye to wo jawab
    adhoori baat par bana hai — bhejna nahi, naye wale ko poori baat ka
    ek jawab dene do. Yahi Sakshi wali asli timeline hai: msg1 05:42:15,
    msg2 05:42:22, reply1 05:42:26 (adhoora), reply2 05:42:37 (poora).
    """
    import app.routers.webhook as wh
    from app.models import Direction

    async def slow_ai_reply(db, customer, text, **kw):
        # LLM ke sochne ke dauraan doosra message aa gaya — wahi race
        db.add(
            Conversation(
                customer_id=customer.id,
                direction=Direction.INBOUND,
                message_text="3 dryclean",
                wa_message_id="wamid.TESTco-race2",
            )
        )
        await db.commit()
        return "11 items ka jawab (adhoora)"

    monkeypatch.setattr(wh, "build_ai_reply", slow_ai_reply)

    body = meta_payload(
        messages=[{
            "from": TEST_CUSTOMER_PHONE_RAW,
            "id": "wamid.TESTco-race1",
            "type": "text",
            "text": {"body": "11 iron"},
        }]
    )
    r = await client.post(
        "/webhook", content=body, headers={"X-Hub-Signature-256": sign_body(body)}
    )
    assert r.status_code == 200
    replies = [m for m in sent if m["to"] == TEST_CUSTOMER_PHONE]
    assert replies == [], f"adhoore jawab ko rukna chahiye tha: {replies}"


async def test_single_message_still_replies_instantly(client, sent) -> None:
    """Coalescing sirf jaldi-jaldi wale messages par — akela message
    pehle jaisa turant jawab paata hai (upar wala inbound test bhi yahi
    dekhta hai; ye uska saaf naam wala prahari hai)."""
    body = meta_payload(
        messages=[{
            "from": TEST_CUSTOMER_PHONE_RAW,
            "id": "wamid.TESTco-solo",
            "type": "text",
            "text": {"body": "kya haal"},
        }]
    )
    r = await client.post(
        "/webhook", content=body, headers={"X-Hub-Signature-256": sign_body(body)}
    )
    assert r.status_code == 200
    replies = [m for m in sent if m["to"] == TEST_CUSTOMER_PHONE]
    assert len(replies) == 1
