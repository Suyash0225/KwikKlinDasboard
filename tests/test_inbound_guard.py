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
        "Kapde pickup karwana hai, Varanasi mein service hai?"
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
async def test_classifier_failure_allows_normal_customer_flow(monkeypatch):
    async def fake_ask_json(**kwargs):
        raise RuntimeError("test failure")

    monkeypatch.setattr(inbound_guard.llm_client, "ask_json", fake_ask_json)

    assert not await inbound_guard.should_suppress_inbound(
        "Mujhe laundry service chahiye."
    )


@pytest.mark.asyncio
async def test_clear_customer_enquiry_is_a_lead_without_llm(monkeypatch):
    async def fail_if_called(**kwargs):
        raise AssertionError("clear customer enquiry should use the deterministic fast path")
    monkeypatch.setattr(inbound_guard.llm_client, "ask_json", fail_if_called)
    result = await inbound_guard.classify_inbound_message("Mujhe laundry service chahiye.")
    assert result["classification"] == "CUSTOMER_LEAD"
    assert inbound_guard.is_customer_lead(result)


@pytest.mark.asyncio
async def test_greeting_and_classifier_failure_never_become_leads(monkeypatch):
    async def fail_classifier(**kwargs):
        raise RuntimeError("classifier unavailable")
    monkeypatch.setattr(inbound_guard.llm_client, "ask_json", fail_classifier)
    greeting = await inbound_guard.classify_inbound_message("Hello, are you available?")
    assert greeting["classification"] == "GENERAL_MESSAGE"
    assert not inbound_guard.is_customer_lead(greeting)


@pytest.mark.asyncio
async def test_order_support_is_not_a_new_sales_lead():
    result = await inbound_guard.classify_inbound_message("Mera order kahan hai?")
    assert result["classification"] == "CUSTOMER_SERVICE"
    assert not inbound_guard.is_customer_lead(result)
