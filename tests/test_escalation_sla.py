"""Customer acknowledgement and owner SLA reminder regression tests."""
from datetime import datetime, timedelta, timezone
import uuid

from app.database import async_session_factory
from app.models import Customer, Escalation
from app.services import escalation as escalation_service, tenant_context


async def test_escalation_acknowledges_customer_and_reminds_owner_once(monkeypatch) -> None:
    home = await tenant_context.get_home_tenant_id()
    sent = []

    async def fake_send(db, *, to_phone, text=None, **kwargs):
        sent.append({"to": to_phone, "text": text or "", **kwargs})
        return "wamid.test"

    async def fake_recipients(db):
        return [("+919999000001", "Owner")]

    monkeypatch.setattr(escalation_service, "send_message", fake_send)
    from app.services import team
    monkeypatch.setattr(team, "alert_recipients", fake_recipients)

    phone = f"+91{uuid.uuid4().int % 10**10:010d}"
    async with tenant_context.as_tenant(home):
        async with async_session_factory() as db:
            customer = Customer(phone=phone, name="Complaint Test")
            db.add(customer)
            await db.flush()
            case = await escalation_service.raise_escalation(
                db, customer=customer, question="Delivery was late and nobody called."
            )
            assert case is not None
            assert any(m["to"] == phone and "passed to the shop owner" in m["text"] for m in sent)
            assert any(m["to"] == "+919999000001" and "Delivery was late" in m["text"] for m in sent)

            now = datetime.now(timezone.utc)
            case.created_at = now - timedelta(hours=4)
            await db.commit()
            before = len(sent)
            count = await escalation_service.remind_overdue_escalations(db, now=now)
            assert count == 1
            reminder_messages = sent[before:]
            assert any(m["to"] == "+919999000001" and "SLA REMINDER" in m["text"] for m in reminder_messages)

            before = len(sent)
            count_again = await escalation_service.remind_overdue_escalations(db, now=now + timedelta(hours=1))
            assert count_again == 0
            assert len(sent) == before
