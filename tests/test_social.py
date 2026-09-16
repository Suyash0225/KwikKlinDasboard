"""Daily social poster: generation, owner pack, IG skip, idempotency."""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text as sqltext

import app.services.social as social_module
from app.database import async_session_factory
from app.services import app_settings
from app.services.social import (
    GOOGLE_POST_RULES,
    MEDIA_DIR,
    check_google_image,
    draw_poster,
    enforce_google_rules,
    google_image_url,
    google_post_text,
    run_daily_social,
    too_similar,
)

IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture(autouse=True)
async def _cleanup():
    # Tests live DB par chalte hain — owner ki asli Google post history wapas rakho
    async with async_session_factory() as s:
        history = await app_settings.get(s, "google_post_history")
    yield
    day = datetime.now(IST).strftime("%Y-%m-%d")
    (MEDIA_DIR / f"social-{day}.png").unlink(missing_ok=True)
    (MEDIA_DIR / f"social-google-{day}.png").unlink(missing_ok=True)
    async with async_session_factory() as s:
        await s.execute(sqltext("DELETE FROM sent_events WHERE event_key LIKE 'social:%'"))
        await s.commit()
        await app_settings.set_value(s, "google_post_history", history)


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

    # Google pack: apna poster + rules wala text, jisme number/hashtag nahi
    assert (MEDIA_DIR / f"social-google-{day}.png").exists()
    google_msg = next(s for s in sends if "Google Business post" in s)
    body = google_msg.split("(copy karke daal do):", 1)[1].split("✔️", 1)[0]
    assert "96968" not in body and "#" not in body and "wa.me" not in body
    assert "review karta hai" in google_msg  # moderation delay bataya

    # same day again -> claimed, nothing re-sent
    before = len(sends)
    assert await run_daily_social() == "already_done"
    assert len(sends) == before


def test_google_rules_are_in_the_ai_prompt() -> None:
    rules = GOOGLE_POST_RULES.lower()
    assert "phone number" in rules and "never repeat" in rules and "hashtags" in rules


def test_enforce_google_rules_strips_numbers_links_and_hashtags() -> None:
    draft = (
        "Fresh clothes for Diwali in Varanasi!\n"
        "📲 WhatsApp: +91 96968 56069\n"
        "Call us at 9696856069 or visit https://wa.me/919696856069\n"
        "Book a pickup today. #KwikKlin #Laundry"
    )
    out = enforce_google_rules(draft)
    assert "96968" not in out and "9696856069" not in out
    assert "wa.me" not in out and "http" not in out and "#" not in out
    assert "WhatsApp" not in out  # label ke saath bacha "WhatsApp:" bhi gaya
    assert out.startswith("Fresh clothes for Diwali in Varanasi!")
    assert "Book a pickup today." in out


def test_too_similar_catches_repeats_but_allows_fresh_text() -> None:
    old = ["Monday Fresh Start: fresh, clean clothes without leaving home. Book a pickup today."]
    assert too_similar("Monday Fresh Start: fresh, clean clothes without leaving home! Book today.", old)
    assert not too_similar("Heavy blankets and quilts need deep cleaning before winter in Varanasi.", old)


async def test_google_post_never_repeats_recent_post(monkeypatch) -> None:
    async with async_session_factory() as db:
        await app_settings.set_value(db, "google_post_history", [
            {"date": "2026-01-01", "text": "Same text every day, Varanasi laundry. Book a pickup today."},
        ])
        drafts = iter([
            "Same text every day, Varanasi laundry. Book a pickup today.",  # repeat -> rejected
            "Winter is here: we deep clean quilts and blankets in Varanasi. Book a pickup today.",
        ])
        calls = []

        async def fake_ask(**kw):
            calls.append(kw)
            return next(drafts)

        monkeypatch.setattr("app.services.llm_client.ask", fake_ask)
        text = await google_post_text(db, "Kambal-Razai Care", "h", "s", "2026-01-02")
        assert text.startswith("Winter is here")
        assert "too similar" in calls[1]["user_text"]  # AI ko dobara likhne ko kaha
        assert "phone number" in calls[0]["system"].lower()  # rules prompt mein gaye
        hist = await app_settings.get(db, "google_post_history")
        assert hist[-1] == {"date": "2026-01-02", "text": text}


def test_google_poster_has_no_phone_and_meets_image_rules(tmp_path: Path) -> None:
    out = tmp_path / "g.png"
    draw_poster("Test Theme", "Saaf kapde", "Wash & Iron", out, show_phone=False)
    assert check_google_image(out) == []
    tiny = tmp_path / "tiny.png"
    from PIL import Image

    Image.new("RGB", (100, 100), "white").save(tiny)
    assert any("250x250" in p for p in check_google_image(tiny))


def test_google_image_url_needs_a_fixed_https_domain() -> None:
    assert google_image_url("https://kwikklin.in", True, "x.png") == ("https://kwikklin.in/social/x.png", "")
    assert google_image_url("https://abc.trycloudflare.com", True, "x.png")[1]
    assert google_image_url("https://kwikklin.in", False, "x.png")[1]
    assert google_image_url("http://127.0.0.1:8000", True, "x.png")[1]


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
