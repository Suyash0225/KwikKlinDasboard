"""Inbox ✨ AI reply suggestions (services/inbox_suggest.py)."""

from datetime import datetime, timedelta, timezone

import app.services.llm_client as llm
from app.config import settings
from app.database import async_session_factory
from app.models import Conversation, Customer, Direction
from tests.conftest import purge_phones

H = {"X-API-Key": settings.ADMIN_API_KEY}
PHONE = "+919999900456"


class _Resp:
    status_code = 200

    def __init__(self, text: str):
        self._text = text

    def json(self):
        return {"candidates": [{"content": {"parts": [{"text": self._text}]}}], "usageMetadata": {}}


async def test_deep_thinking_raises_thinking_only_on_the_smart_model(monkeypatch) -> None:
    seen = []

    async def fake_post(model, payload):
        seen.append((model, payload["generationConfig"]["thinkingConfig"]["thinkingLevel"]))
        return _Resp("ok")

    monkeypatch.setattr(llm, "_gemini_post", fake_post)
    await llm._gemini_generate("sys", "hi", llm.MODEL_SMART, 100, None, None)
    with llm.deep_thinking():
        await llm._gemini_generate("sys", "hi", llm.MODEL_SMART, 100, None, None)
        await llm._gemini_generate("sys", "hi", llm.MODEL_CHEAP, 100, None, None)
    await llm._gemini_generate("sys", "hi", llm.MODEL_SMART, 100, None, None)
    assert [lvl for _, lvl in seen] == ["low", "high", "minimal", "low"]


async def test_suggest_reads_the_whole_chat_and_sends_nothing(client, sent, monkeypatch) -> None:
    await purge_phones(PHONE)
    now = datetime.now(timezone.utc)
    async with async_session_factory() as db:
        cust = Customer(phone=PHONE, name="Chup Grahak")
        db.add(cust)
        await db.flush()
        for i, (d, t) in enumerate([
            (Direction.INBOUND, "Saree dry clean ka rate kya hai?"),
            (Direction.OUTBOUND, "Saree dry clean ₹250 hai ji"),
            (Direction.INBOUND, "Theek hai, sochta hoon"),
        ]):
            db.add(Conversation(customer_id=cust.id, direction=d, message_text=t,
                                created_at=now - timedelta(days=3, minutes=10 - i)))
        await db.commit()

    calls = []

    async def fake_ask_json(**kw):
        calls.append(kw)
        return {
            "situation": "Asked the saree rate, then went quiet.",
            "suggestions": [
                {"label": "Pickup offer", "text": "Namaste ji, kal saree pickup karwa doon?"},
                {"label": "", "text": "  "},
            ],
        }

    monkeypatch.setattr(llm, "ask_json", fake_ask_json)
    try:
        r = await client.post("/admin/api/inbox/suggest", json={"phone": PHONE}, headers=H)
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["situation"].startswith("Asked the saree rate")
        assert out["suggestions"] == [{"label": "Pickup offer", "text": "Namaste ji, kal saree pickup karwa doon?"}]
        prompt = calls[0]["user_text"]
        assert "Saree dry clean ka rate kya hai?" in prompt and "Theek hai, sochta hoon" in prompt
        assert prompt.index("rate kya hai") < prompt.index("sochta hoon"), "oldest first"
        assert "3 day(s) ago" in prompt
        assert calls[0]["model"] == llm.MODEL_SMART
        assert sent == [], "suggestions must never send anything"
    finally:
        await purge_phones(PHONE)


async def test_suggest_is_for_customer_chats_only(client) -> None:
    r = await client.post("/admin/api/inbox/suggest", json={"phone": "+919999900457"}, headers=H)
    assert r.status_code == 404
