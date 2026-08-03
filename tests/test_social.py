"""Daily social poster: generation, owner pack, IG skip, idempotency."""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text as sqltext

import app.services.social as social_module
from app.database import async_session_factory
from app.services.social import MEDIA_DIR, draw_poster, run_daily_social

IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture(autouse=True)
async def _cleanup():
    yield
    day = datetime.now(IST).strftime("%Y-%m-%d")
    (MEDIA_DIR / f"social-{day}.png").unlink(missing_ok=True)
    async with async_session_factory() as s:
        await s.execute(sqltext("DELETE FROM sent_events WHERE event_key LIKE 'social:%'"))
        await s.commit()


def test_poster_draws(tmp_path: Path) -> None:
    out = tmp_path / "p.png"
    draw_poster("Test Theme", "Saaf kapde, khush aap", "Wash & Iron", out)
    assert out.exists() and out.stat().st_size > 10_000  # real image, not a stub


async def test_daily_social_flow_and_idempotency(monkeypatch) -> None:
    sends: list[str] = []

    async def fake_send_image(db, **kw):
        sends.append("image:" + (kw.get("caption") or ""))
        return "wamid.SOCIAL-IMG"

    async def fake_send_message(db, *, to_phone, text=None, **kw):
        sends.append("text:" + (text or ""))
        return "wamid.SOCIAL-TXT"

    monkeypatch.setattr(social_module, "__test_guard", True, raising=False)
    monkeypatch.setattr("app.services.whatsapp.send_image", fake_send_image)
    monkeypatch.setattr("app.services.whatsapp.send_message", fake_send_message)

    status = await run_daily_social()
    # IG not configured in tests -> skipped; LLM firewalled -> fallback caption
    assert status == "skipped"
    day = datetime.now(IST).strftime("%Y-%m-%d")
    assert (MEDIA_DIR / f"social-{day}.png").exists()
    assert any(s.startswith("image:") for s in sends)
    joined = " ".join(sends)
    assert "WhatsApp: +91 96968 56069" in joined and "#KwikKlin" in joined

    # same day again -> claimed, nothing re-sent
    before = len(sends)
    assert await run_daily_social() == "already_done"
    assert len(sends) == before


async def test_social_route_serves_only_social_files(client) -> None:
    MEDIA_DIR.mkdir(exist_ok=True)
    f = MEDIA_DIR / "social-testfile.png"
    f.write_bytes(b"\x89PNG-fake")
    try:
        r = await client.get("/social/social-testfile.png")
        assert r.status_code == 200
        # anything else in media/ stays private
        r2 = await client.get("/social/in-somechat.jpg")
        assert r2.status_code == 404
        r3 = await client.get("/social/..%2F..%2F.env")
        assert r3.status_code == 404
    finally:
        f.unlink(missing_ok=True)
