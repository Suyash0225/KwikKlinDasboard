import pytest

from app.services import waha


@pytest.mark.asyncio
async def test_resolve_lid_returns_phone_jid(monkeypatch):
    async def fake_get(path):
        assert "/lids/123456@lid" in path
        return {"lid": "123456@lid", "pn": "919876543210@c.us"}

    monkeypatch.setattr(waha, "_get", fake_get)
    assert await waha.resolve_lid("123456@lid") == "919876543210@c.us"


@pytest.mark.asyncio
async def test_resolve_lid_returns_none_when_mapping_missing(monkeypatch):
    async def fake_get(path):
        return {"lid": "123456@lid", "pn": None}

    monkeypatch.setattr(waha, "_get", fake_get)
    assert await waha.resolve_lid("123456@lid") is None


def test_waha_shape_uses_resolved_phone():
    from app.routers.webhook import _waha_to_meta_shape

    msg = _waha_to_meta_shape(
        {"from": "123456@lid", "id": "wamid-1", "body": "hello"},
        resolved_from="919876543210@c.us",
    )
    assert msg["from"] == "919876543210"
