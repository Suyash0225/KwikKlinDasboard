from app.services.gbp_owner_post import _caption_and_cta


def test_owner_caption_defaults_to_website_cta():
    caption, action = _caption_and_cta("Fresh clothes, less hassle in Varanasi.")
    assert caption == "Fresh clothes, less hassle in Varanasi."
    assert action == "LEARN_MORE"


def test_owner_caption_can_select_call_cta():
    caption, action = _caption_and_cta("CALL NOW: Book a laundry pickup in Varanasi.")
    assert caption == "Book a laundry pickup in Varanasi."
    assert action == "CALL"


def test_owner_caption_can_select_website_cta():
    caption, action = _caption_and_cta("VISIT WEBSITE: Book online with Kwik Klin.")
    assert caption == "Book online with Kwik Klin."
    assert action == "LEARN_MORE"


def test_google_policy_removes_phone_number_from_caption():
    caption, action = _caption_and_cta("WEBSITE: Call us at +91 96968 56069 for pickup.")
    assert "+91 96968 56069" not in caption
    assert action == "LEARN_MORE"


def test_verified_owner_phone_accepts_waha_jid_format():
    from app.services.gbp_owner_post import _same_phone

    assert _same_phone("919696856069@c.us", "+91 96968 56069")
    assert not _same_phone("919696856068@c.us", "+91 96968 56069")



async def test_owner_caption_uses_smart_model_and_enforces_google_policy(monkeypatch):
    from app.services import gbp_owner_post, llm_client

    captured = {}

    async def fake_ask_json(**kwargs):
        captured.update(kwargs)
        return {"summary": "Celebrate Dussehra with fresh, well-cared-for clothes in Varanasi. Call +91 96968 56069."}

    monkeypatch.setattr(llm_client, "ask_json", fake_ask_json)
    caption = await gbp_owner_post._interpret_owner_caption(
        "Dussehra wish, laundry care in Varanasi"
    )

    assert "Dussehra" in caption
    assert "96968" not in caption
    assert captured["model"] == llm_client.MODEL_SMART
    assert captured["schema"]["required"] == ["summary"]


async def test_owner_caption_falls_back_to_owner_copy_when_ai_unavailable(monkeypatch):
    from app.services import gbp_owner_post, llm_client

    async def unavailable(**kwargs):
        raise llm_client.LLMUnavailable("provider unavailable")

    monkeypatch.setattr(llm_client, "ask_json", unavailable)
    caption = await gbp_owner_post._interpret_owner_caption(
        "Fresh laundry care in Varanasi"
    )
    assert caption == "Fresh laundry care in Varanasi"
