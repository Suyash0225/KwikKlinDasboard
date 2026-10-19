"""Secret settings must be decrypted consistently by batched reads."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.services import app_settings


async def test_get_many_decrypts_instagram_access_token(monkeypatch):
    monkeypatch.setattr(app_settings, "_effective_tenant_id", lambda: "tenant-id")
    monkeypatch.setattr(
        app_settings,
        "decrypt",
        lambda value: "plain-token" if value == "enc:test-token" else value,
    )
    row = SimpleNamespace(
        key="ig_access_token",
        value={"v": "enc:test-token"},
    )
    scalars = SimpleNamespace(all=lambda: [row])
    db = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: scalars))
    )

    result = await app_settings.get_many(db, "ig_access_token")

    assert result["ig_access_token"] == "plain-token"
