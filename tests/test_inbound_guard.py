import pytest

from app.services import inbound_guard


@pytest.mark.asyncio
async def test_business_advertisement_is_suppressed(monkeypatch):
    async def fake_ask_json(**kwargs):
        return {
            "classification": "BUSINESS_ADVERTISEMENT",
            "confidence": 0.96,
            "reason": "Promotes digital marketing services",
        }

    monkeypatch.setattr(inbound_guard.llm_client, "ask_json", fake_ask_json)

    assert await inbound_guard.should_suppress_inbound(
        "Hello, we provide digital marketing and SEO services for businesses."
    )


@pytest.mark.asyncio
async def test_vendor_message_is_suppressed(monkeypatch):
    async def fake_ask_json(**kwargs):
        return {
            "classification": "VENDOR_OR_PARTNER",
            "confidence": 0.93,
            "reason": "Vendor is selling software development services",
        }

    monkeypatch.setattr(inbound_guard.llm_client, "ask_json", fake_ask_json)

    assert await inbound_guard.should_suppress_inbound(
        "We develop websites and software for businesses. Can we work with you?"
    )


@pytest.mark.asyncio
async def test_genuine_laundry_customer_is_not_suppressed(monkeypatch):
    async def fake_ask_json(**kwargs):
        return {
            "classification": "CUSTOMER_LEAD",
            "confidence": 0.99,
            "reason": "Customer asks for laundry pickup",
        }

    monkeypatch.setattr(inbound_guard.llm_client, "ask_json", fake_ask_json)

    assert not await inbound_guard.should_suppress_inbound(
        "Do you offer laundry cleaning in Varanasi?"
    )


@pytest.mark.asyncio
async def test_ambiguous_message_is_not_suppressed(monkeypatch):
    async def fake_ask_json(**kwargs):
        return {
            "classification": "BUSINESS_ADVERTISEMENT",
            "confidence": 0.61,
            "reason": "Could be a promotional service message",
        }

    monkeypatch.setattr(inbound_guard.llm_client, "ask_json", fake_ask_json)

    assert not await inbound_guard.should_suppress_inbound(
        "Hello, we have a service for your business."
    )


@pytest.mark.asyncio
async def test_clear_customer_enquiry_does_not_need_classifier(monkeypatch):
    async def fake_ask_json(**kwargs):
        raise AssertionError("clear customer enquiries should use the deterministic fast path")

    monkeypatch.setattr(inbound_guard.llm_client, "ask_json", fake_ask_json)

    result = await inbound_guard.classify_inbound_message("Mujhe laundry service chahiye.")
    assert result["classification"] == "CUSTOMER_LEAD"
    assert inbound_guard.is_customer_lead(result)
    assert not await inbound_guard.should_suppress_inbound("Mujhe laundry service chahiye.")



@pytest.mark.asyncio
async def test_marketing_pitch_without_old_trigger_words_is_classified(monkeypatch):
    """A sales pitch must be classified even when it has no URL/SEO keyword."""
    calls = []

    async def fake_ask_json(**kwargs):
        calls.append(kwargs["user_text"])
        return {
            "classification": "BUSINESS_ADVERTISEMENT",
            "confidence": 0.97,
            "reason": "Offers business growth services",
        }

    monkeypatch.setattr(inbound_guard.llm_client, "ask_json", fake_ask_json)

    message = "Hello sir, we can help you grow your business and get more customers."
    result = await inbound_guard.classify_inbound_message(message)

    assert calls, "non-obvious messages must go through intent classification"
    assert result["classification"] == "BUSINESS_ADVERTISEMENT"
    assert inbound_guard.is_suppressed_classification(result)
    assert not inbound_guard.is_customer_lead(result)


@pytest.mark.asyncio
async def test_generic_greeting_is_not_a_lead(monkeypatch):
    async def fake_ask_json(**kwargs):
        return {
            "classification": "GENERAL_MESSAGE",
            "confidence": 0.99,
            "reason": "Greeting without a service request",
        }

    monkeypatch.setattr(inbound_guard.llm_client, "ask_json", fake_ask_json)
    result = await inbound_guard.classify_inbound_message("Hello there")

    assert result["classification"] == "GENERAL_MESSAGE"
    assert not inbound_guard.is_customer_lead(result)
    assert not inbound_guard.is_suppressed_classification(result)


@pytest.mark.asyncio
async def test_order_support_is_not_a_new_sales_lead():
    result = await inbound_guard.classify_inbound_message("Mera order kahan hai?")

    assert result["classification"] == "CUSTOMER_SERVICE"
    assert not inbound_guard.is_customer_lead(result)


@pytest.mark.asyncio
async def test_classifier_failure_does_not_promote_vague_message_to_lead(monkeypatch):
    async def fake_ask_json(**kwargs):
        raise RuntimeError("test failure")

    monkeypatch.setattr(inbound_guard.llm_client, "ask_json", fake_ask_json)
    result = await inbound_guard.classify_inbound_message("Hello, are you available?")

    assert result["classification"] == "GENERAL_MESSAGE"
    assert not inbound_guard.is_customer_lead(result)


@pytest.mark.asyncio
async def test_classifier_failure_still_suppresses_unmistakable_marketing(monkeypatch):
    async def fake_ask_json(**kwargs):
        raise RuntimeError("test failure")

    monkeypatch.setattr(inbound_guard.llm_client, "ask_json", fake_ask_json)

    assert await inbound_guard.should_suppress_inbound(
        "Hello, we provide digital marketing and SEO services for businesses."
    )
