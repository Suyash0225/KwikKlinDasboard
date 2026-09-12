"""tenants.wa_token DB mein encrypted, Python mein plain (P3)."""

import pytest
from sqlalchemy import text as sqltext

from app.database import async_session_factory
from app.models.tenant import Tenant
from app.services import secrets, tenant_context

SLUG = "test-enc-a"
TOKEN = "EAAplainTokenForEncryptionTest0001"


@pytest.fixture
def fernet_key(monkeypatch):
    from cryptography.fernet import Fernet

    from app.config import settings

    monkeypatch.setattr(settings, "TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(secrets, "_checked", False)
    monkeypatch.setattr(secrets, "_fernet", None)
    yield
    monkeypatch.setattr(secrets, "_checked", False)
    monkeypatch.setattr(secrets, "_fernet", None)


@pytest.fixture
async def tenant():
    async with async_session_factory() as db:
        await db.execute(sqltext("DELETE FROM tenants WHERE slug = :s"), {"s": SLUG})
        await db.commit()
    try:
        yield
    finally:
        tenant_context.current_tenant_id.set(None)
        async with async_session_factory() as db:
            await db.execute(sqltext("DELETE FROM tenants WHERE slug = :s"), {"s": SLUG})
            await db.commit()


async def test_token_is_encrypted_in_db_and_plain_in_python(fernet_key, tenant) -> None:
    async with async_session_factory() as db:
        t = Tenant(slug=SLUG, shop_name="Enc A", owner_name="A", plan="starter",
                   owner_phone="+919999900501", status="active",
                   wa_phone_number_id="777000111222", wa_token=TOKEN)
        db.add(t)
        await db.commit()
        tid = t.id
    # DB mein raw column: prefix ke saath, asli token kahin nahi
    async with async_session_factory() as db:
        raw = (await db.execute(sqltext("SELECT wa_token FROM tenants WHERE id = :i"), {"i": tid})).scalar_one()
    assert raw.startswith("enc:v1:") and TOKEN not in raw
    # ORM se: plain
    async with async_session_factory() as db:
        t = await db.get(Tenant, tid)
        assert t.wa_token == TOKEN


async def test_legacy_plaintext_row_still_reads_and_upgrades(fernet_key, tenant) -> None:
    """Feature se pehle ki plaintext row: padhne par wahi, script chalane
    par encrypted."""
    async with async_session_factory() as db:
        t = Tenant(slug=SLUG, shop_name="Enc A", owner_name="A", plan="starter",
                   owner_phone="+919999900501", status="active")
        db.add(t)
        await db.commit()
        tid = t.id
        await db.execute(sqltext("UPDATE tenants SET wa_token = :v WHERE id = :i"), {"v": TOKEN, "i": tid})
        await db.commit()
    async with async_session_factory() as db:
        assert (await db.get(Tenant, tid)).wa_token == TOKEN
    from scripts.encrypt_tokens import main as encrypt_all

    await encrypt_all()
    async with async_session_factory() as db:
        raw = (await db.execute(sqltext("SELECT wa_token FROM tenants WHERE id = :i"), {"i": tid})).scalar_one()
        assert raw.startswith("enc:v1:")
        assert (await db.get(Tenant, tid)).wa_token == TOKEN


def test_wrong_key_fails_loudly(fernet_key) -> None:
    enc = secrets.encrypt("hello")
    assert enc.startswith("enc:v1:")
    from cryptography.fernet import Fernet

    secrets._fernet = Fernet(Fernet.generate_key())
    with pytest.raises(RuntimeError, match="does not match"):
        secrets.decrypt(enc)


def test_no_key_means_plaintext_with_warning(monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "TOKEN_ENCRYPTION_KEY", "")
    monkeypatch.setattr(secrets, "_checked", False)
    monkeypatch.setattr(secrets, "_fernet", None)
    assert secrets.encrypt("abc") == "abc"
    assert secrets.decrypt("abc") == "abc"
