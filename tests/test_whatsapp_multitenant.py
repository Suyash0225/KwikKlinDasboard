"""Multi-tenant WhatsApp proof (official Meta Cloud API — unofficial kuch
hai hi nahi is project mein).

1. Inbound routing: webhook ka value.metadata.phone_number_id jis tenant ka
   connected number hai, message USI tenant_id ke saath store hota hai.
2. Home/env number (aur DotPe ka khali metadata) -> home tenant (legacy path
   bilkul waisa hi).
3. Anjaan phone_number_id -> process SKIP (galat tenant mein store karne se
   behtar) + loud log.
4. Outbound: current tenant ke DB creds (token/number) use hote hain;
   DB creds nahi to .env fallback.
5. Connect sirf Control panel se (tests/test_client_ops.py, test_template_studio.py).
"""

import pytest
from sqlalchemy import text as sqltext

from app.config import settings
from app.database import async_session_factory
from app.models.tenant import Tenant, User
from app.services import auth, tenant_context, whatsapp
from tests.conftest import meta_payload, sign_body

B_SLUG = "test-wa-b"
B_PNID = "555000111222"
B_TOKEN = "EAAtestTokenForTenantB0000000000"
B_CUSTOMER = "+919999900086"
B_EMAIL = "wa-b@test.local"


@pytest.fixture
async def wa_tenant_b():
    async with async_session_factory() as db:
        t = Tenant(
            slug=B_SLUG, shop_name="WA Test B", owner_name="B", plan="growth",
            owner_phone="+919999900087", status="active",
            wa_phone_number_id=B_PNID, wa_waba_id="waba123", wa_token=B_TOKEN,
        )
        db.add(t)
        await db.commit()
        await db.refresh(t)
    try:
        yield t
    finally:
        tenant_context.current_tenant_id.set(None)
        async with async_session_factory() as db:
            # tenant B par stamp hui HAR cheez — warna tenants delete FK par girta hai
            for tbl in ("audit_log", "llm_usage", "conversations", "escalations",
                        "open_questions", "tasks", "customers", "leads"):
                await db.execute(
                    sqltext(
                        f"DELETE FROM {tbl} WHERE tenant_id IN "
                        "(SELECT id FROM tenants WHERE slug = :s)"
                    ), {"s": B_SLUG},
                )
            await db.execute(
                sqltext(
                    "DELETE FROM conversations WHERE customer_id IN "
                    "(SELECT id FROM customers WHERE phone = :p)"
                ), {"p": B_CUSTOMER},
            )
            await db.execute(
                sqltext("DELETE FROM customers WHERE phone = :p"), {"p": B_CUSTOMER}
            )
            await db.execute(
                sqltext(
                    "DELETE FROM webhook_events WHERE payload::text LIKE :m"
                ), {"m": f"%{B_PNID}%"},
            )
            await db.execute(
                sqltext(
                    "DELETE FROM login_sessions WHERE user_id IN "
                    "(SELECT id FROM users WHERE email = :e)"
                ), {"e": B_EMAIL},
            )
            await db.execute(sqltext("DELETE FROM users WHERE email = :e"), {"e": B_EMAIL})
            await db.execute(sqltext("DELETE FROM tenants WHERE slug = :s"), {"s": B_SLUG})
            await db.commit()


def _payload_for_pnid(pnid: str, from_phone: str, wamid: str) -> bytes:
    import json

    value = {
        "messaging_product": "whatsapp",
        "metadata": {"phone_number_id": pnid},
        "messages": [{
            "from": from_phone.lstrip("+"), "id": wamid,
            "type": "text", "text": {"body": "namaste"},
        }],
    }
    return json.dumps({
        "object": "whatsapp_business_account",
        "entry": [{"id": "e1", "changes": [{"field": "messages", "value": value}]}],
    }).encode()


async def test_inbound_routed_to_owning_tenant(client, sent, wa_tenant_b) -> None:
    import uuid as _uuid

    # unique wamid har run — journal ka body-hash dedup purane run se na takraye
    body = _payload_for_pnid(B_PNID, B_CUSTOMER, f"wamid.WATEST-{_uuid.uuid4().hex[:10]}")
    r = await client.post(
        "/webhook", content=body, headers={"X-Hub-Signature-256": sign_body(body)}
    )
    assert r.status_code == 200
    async with async_session_factory() as db:
        row = (
            await db.execute(
                sqltext(
                    "SELECT c.tenant_id, conv.tenant_id FROM customers c "
                    "JOIN conversations conv ON conv.customer_id = c.id "
                    "WHERE c.phone = :p"
                ), {"p": B_CUSTOMER},
            )
        ).first()
    assert row is not None, "message store hi nahi hua"
    assert str(row[0]) == str(wa_tenant_b.id), "customer galat tenant mein!"
    assert str(row[1]) == str(wa_tenant_b.id), "conversation galat tenant mein!"


async def test_home_number_still_routes_home(client, sent) -> None:
    home = await tenant_context.get_home_tenant_id()
    body = meta_payload(messages=[{
        "from": "919999900011", "id": "wamid.WATEST-HOME",
        "type": "text", "text": {"body": "home test"},
    }])
    r = await client.post(
        "/webhook", content=body, headers={"X-Hub-Signature-256": sign_body(body)}
    )
    assert r.status_code == 200
    async with async_session_factory() as db:
        tid = (
            await db.execute(
                sqltext("SELECT tenant_id FROM customers WHERE phone = '+919999900011'")
            )
        ).scalar()
    assert str(tid) == str(home)
    from tests.conftest import purge_phones

    await purge_phones("+919999900011")


async def test_unknown_phone_number_id_is_skipped(client, sent) -> None:
    body = _payload_for_pnid("999888777000", "+919999900088", "wamid.WATEST-UNK")
    r = await client.post(
        "/webhook", content=body, headers={"X-Hub-Signature-256": sign_body(body)}
    )
    assert r.status_code == 200  # Meta ko hamesha 200 — retry storm nahi
    async with async_session_factory() as db:
        n = (
            await db.execute(
                sqltext("SELECT count(*) FROM customers WHERE phone = '+919999900088'")
            )
        ).scalar_one()
        await db.execute(
            sqltext("DELETE FROM webhook_events WHERE payload::text LIKE '%999888777000%'")
        )
        await db.commit()
    assert n == 0, "anjaan number ka message kisi tenant mein ghus gaya!"


async def test_outbound_uses_tenant_creds_with_env_fallback(
    monkeypatch, wa_tenant_b
) -> None:
    seen: list = []

    async def _capture(payload, to_phone, creds=None):
        seen.append(creds)
        return {"messages": [{"id": "wamid.FAKE"}]}

    monkeypatch.setattr(whatsapp, "_post_with_retry", _capture)

    # Tenant B context -> B ke DB creds
    tok = tenant_context.current_tenant_id.set(wa_tenant_b.id)
    try:
        async with async_session_factory() as db:
            creds = await whatsapp.resolve_creds(db)
        assert creds.token == B_TOKEN and creds.phone_number_id == B_PNID
        assert B_PNID in creds.messages_url
    finally:
        tenant_context.current_tenant_id.reset(tok)

    # System/home context -> .env fallback (home ne apna number DB mein nahi joda)
    async with async_session_factory() as db:
        creds = await whatsapp.resolve_creds(db)
    assert creds.phone_number_id == settings.WHATSAPP_PHONE_NUMBER_ID
    assert creds.token == settings.WHATSAPP_TOKEN
