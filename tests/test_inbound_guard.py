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
